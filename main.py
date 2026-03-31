from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.config import GridConfig, PairRules
from src.portfolio_runtime import TRACKED_PAIRS, compute_runtime_sizing, fetch_portfolio_snapshot
from src.roostoo_client import RoostooClient
from src.strategy import GridStrategy, TickerView

log = logging.getLogger("main")


def pair_rules_from_exchange_info(exchange_info: dict, pair: str) -> PairRules:
    raw = exchange_info["TradePairs"][pair]
    return PairRules(
        pair=pair,
        price_precision=int(raw["PricePrecision"]),
        amount_precision=int(raw["AmountPrecision"]),
        min_order_value=float(raw["MiniOrder"]),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default=None)
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument("--env", dest="env_files", action="append", default=[])
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def load_environment(env_files: list[str]) -> None:
    if not env_files:
        load_dotenv()
        return
    for env_file in env_files:
        load_dotenv(dotenv_path=env_file, override=True)


def compute_desired_map(desired_orders) -> dict[tuple[str, float, float], object]:
    return {(o.side, round(o.price, 8), round(o.quantity, 8)): o for o in desired_orders}


def compute_open_map(open_orders: list[dict]) -> dict[tuple[str, float, float], dict]:
    return {
        (
            str(order.get("Side", "")).upper(),
            round(float(order.get("Price", 0.0)), 8),
            round(float(order.get("Quantity", 0.0)), 8),
        ): order
        for order in open_orders
    }


def all_orders(order_response: dict) -> list[dict]:
    if not order_response.get("Success", True):
        return []
    return list(order_response.get("OrderMatched", []))


def load_portfolio_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {"peak_total_equity_usd": 0.0}
    try:
        data = json.loads(state_path.read_text())
        peak = float(data.get("peak_total_equity_usd", 0.0))
        return {"peak_total_equity_usd": max(peak, 0.0)}
    except Exception:
        log.exception("Failed to load state file %s", state_path)
        return {"peak_total_equity_usd": 0.0}


def save_portfolio_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(state_path)


def update_portfolio_peak(path: str, total_equity_usd: float) -> tuple[float, float]:
    state = load_portfolio_state(path)
    peak = max(float(state.get("peak_total_equity_usd", 0.0)), float(total_equity_usd))
    state["peak_total_equity_usd"] = peak
    save_portfolio_state(path, state)
    if peak <= 0:
        return 0.0, 0.0
    drawdown_pct = total_equity_usd / peak - 1.0
    return peak, drawdown_pct


def get_snapshot_and_sizing(client: RoostooClient, config: GridConfig, ticker: TickerView):
    snapshot = fetch_portfolio_snapshot(
        client,
        max_open_orders=config.max_open_orders,
        own_ticker_mid=ticker.mid,
        own_ticker_change_24h=ticker.change_24h,
        own_pair=config.pair,
        tracked_pairs=TRACKED_PAIRS,
    )
    portfolio_peak_usd, portfolio_drawdown_pct = update_portfolio_peak(
        config.shared_state_file,
        snapshot.total_equity_usd,
    )
    sizing = compute_runtime_sizing(
        config,
        snapshot,
        config.pair,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
    )
    return snapshot, sizing, portfolio_peak_usd


def run_once(client: RoostooClient, config: GridConfig, strategy: GridStrategy, cycle: int) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    log.info("CYCLE %d started at %s", cycle, ts)

    ticker_response = client.ticker(config.pair)
    market = ticker_response["Data"][config.pair]
    ticker = TickerView(
        bid=float(market["MaxBid"]),
        ask=float(market["MinAsk"]),
        last=float(market["LastPrice"]),
        change_24h=float(market["Change"]),
    )
    log.info("TICKER %s", ticker)

    strategy.record_price(ticker.mid)

    if config.pause_guard and strategy.should_pause(ticker):
        log.warning(
            "PAUSED — 24h change %.4f%% exceeds threshold %.4f%%",
            ticker.change_24h * 100,
            config.max_24h_abs_change_pct * 100,
        )
        return {
            "status": "paused",
            "reason": "trend_filter",
            "change_24h": ticker.change_24h,
        }

    snapshot, sizing, portfolio_peak_usd = get_snapshot_and_sizing(client, config, ticker)
    open_orders = snapshot.open_orders_by_pair[config.pair]

    fill_reanchor_triggered = False
    fill_reanchor_price = None
    fill_reanchor_order_id = None

    if config.reanchor_after_fill:
        filled_orders_resp = client.query_orders(
            config.pair,
            pending_only=False,
            limit=max(100, config.max_open_orders),
        )
        new_fills = strategy.consume_new_fills(all_orders(filled_orders_resp))
        if new_fills:
            latest_fill = new_fills[-1]
            fill_reanchor_price = float(latest_fill.get("Price", ticker.mid))
            fill_reanchor_order_id = str(latest_fill.get("OrderID", ""))
            strategy.set_anchor(fill_reanchor_price)
            fill_reanchor_triggered = True
            log.info(
                "REANCHOR_AFTER_FILL triggered | pair=%s order_id=%s side=%s price=%.2f",
                config.pair,
                fill_reanchor_order_id,
                latest_fill.get("Side"),
                fill_reanchor_price,
            )

    buy_locked = sizing.pair_deployable_cash_usd <= 0 or sizing.runtime_buy_order_notional_usd <= 0
    sell_locked = sizing.current_effective_qty <= 0 or sizing.runtime_sell_order_notional_usd <= 0

    refresh_triggered = False
    if strategy.should_refresh(ticker):
        refresh_ok = True

        if strategy.anchor_price is not None:
            if (
                config.disable_downward_refresh_when_no_cash
                and buy_locked
                and ticker.mid < strategy.anchor_price
            ):
                refresh_ok = False

            if (
                config.disable_upward_refresh_when_no_coin
                and sell_locked
                and ticker.mid > strategy.anchor_price
                and sizing.buy_gap_notional_usd <= 0
            ):
                refresh_ok = False

        if refresh_ok:
            log.info(
                "REFRESH triggered — cancelling all %s orders and re-anchoring to mid=%.2f",
                config.pair,
                ticker.mid,
            )
            client.cancel_order(pair=config.pair)
            strategy.set_anchor(ticker.mid)
            refresh_triggered = True

            snapshot, sizing, portfolio_peak_usd = get_snapshot_and_sizing(client, config, ticker)
            open_orders = snapshot.open_orders_by_pair[config.pair]
        else:
            log.info(
                "Refresh blocked | buy_locked=%s sell_locked=%s anchor=%.2f mid=%.2f",
                buy_locked,
                sell_locked,
                strategy.anchor_price,
                ticker.mid,
            )

    desired = strategy.desired_orders(
        ticker=ticker,
        coin_position=sizing.current_effective_qty,
        usd_free=sizing.pair_deployable_cash_usd,
        buy_order_notional_usd=sizing.runtime_buy_order_notional_usd,
        sell_order_notional_usd=sizing.runtime_sell_order_notional_usd,
        max_position_notional_usd=sizing.investable_cap_notional_usd,
    )

    desired_map = compute_desired_map(desired)
    open_map = compute_open_map(open_orders)

    to_cancel = [order for key, order in open_map.items() if key not in desired_map]
    to_cancel_keys = {
        (
            str(order.get("Side", "")).upper(),
            round(float(order.get("Price", 0.0)), 8),
            round(float(order.get("Quantity", 0.0)), 8),
        )
        for order in to_cancel
    }

    reclaim_buy_cash = sum(
        float(order.get("Price", 0.0)) * float(order.get("Quantity", 0.0))
        for order in to_cancel
        if str(order.get("Side", "")).upper() == "BUY"
    )
    reclaim_sell_qty = sum(
        float(order.get("Quantity", 0.0))
        for order in to_cancel
        if str(order.get("Side", "")).upper() == "SELL"
    )

    if to_cancel:
        log.info("Cancelling %d stale orders", len(to_cancel))
        for order in to_cancel:
            try:
                client.cancel_order(order_id=str(order["OrderID"]))
            except Exception:
                log.exception("cancel_order failed for %s", order.get("OrderID"))

    if reclaim_buy_cash > 0 or reclaim_sell_qty > 0:
        desired = strategy.desired_orders(
            ticker=ticker,
            coin_position=sizing.current_effective_qty + reclaim_sell_qty,
            usd_free=sizing.pair_deployable_cash_usd + reclaim_buy_cash,
            buy_order_notional_usd=sizing.runtime_buy_order_notional_usd,
            sell_order_notional_usd=sizing.runtime_sell_order_notional_usd,
            max_position_notional_usd=sizing.investable_cap_notional_usd,
        )
        desired_map = compute_desired_map(desired)

    remaining_open_map = {k: v for k, v in open_map.items() if k not in to_cancel_keys}
    to_place = [order for key, order in desired_map.items() if key not in remaining_open_map]

    placed = 0
    for order in to_place[: config.max_open_orders]:
        try:
            client.place_limit_order(config.pair, order.side, order.quantity, order.price)
            placed += 1
        except Exception:
            log.exception("place_limit_order failed for %s", order)

    result = {
        "status": "ok",
        "mid": ticker.mid,
        "pair_change_24h": sizing.pair_change_24h,
        "portfolio_peak_usd": portfolio_peak_usd,
        "portfolio_drawdown_pct": sizing.portfolio_drawdown_pct,
        "total_equity_usd": sizing.total_equity_usd,
        "target_weight_pct": sizing.target_weight_pct,
        "target_notional_usd": sizing.target_notional_usd,
        "current_notional_usd": sizing.current_notional_usd,
        "buy_gap_notional_usd": sizing.buy_gap_notional_usd,
        "sell_gap_notional_usd": sizing.sell_gap_notional_usd,
        "runtime_buy_order_notional_usd": sizing.runtime_buy_order_notional_usd,
        "runtime_sell_order_notional_usd": sizing.runtime_sell_order_notional_usd,
        "buy_scale": sizing.buy_scale,
        "sell_scale": sizing.sell_scale,
        "buy_room_ratio": sizing.buy_room_ratio,
        "sell_room_ratio": sizing.sell_room_ratio,
        "base_reserve_usd": sizing.reserve_usd,
        "emergency_reserve_usd": sizing.emergency_reserve_usd,
        "emergency_locked_cash_usd": sizing.emergency_locked_cash_usd,
        "emergency_release_fraction": sizing.emergency_release_fraction,
        "wallet_deployable_cash_usd": sizing.wallet_deployable_cash_usd,
        "pair_deployable_cash_usd": sizing.pair_deployable_cash_usd,
        "reclaim_buy_cash_usd": reclaim_buy_cash,
        "reclaim_sell_qty": reclaim_sell_qty,
        "fill_reanchor_triggered": fill_reanchor_triggered,
        "fill_reanchor_price": fill_reanchor_price,
        "fill_reanchor_order_id": fill_reanchor_order_id,
        "refresh_triggered": refresh_triggered,
        "anchor": strategy.anchor_price,
        "desired_orders": len(desired),
        "placed_orders": placed,
    }
    log.info("CYCLE %d RESULT: %s", cycle, json.dumps(result, indent=2))
    return result


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_level)
    load_environment(args.env_files)

    config = GridConfig.from_env()
    if args.pair is not None:
        config.pair = args.pair
    if args.poll_seconds is not None:
        config.poll_seconds = args.poll_seconds

    log.info("Config: %s", json.dumps(asdict(config), indent=2))

    client = RoostooClient()
    rules = pair_rules_from_exchange_info(client.exchange_info(), config.pair)
    strategy = GridStrategy(config, rules)

    cycle = 0
    while True:
        cycle += 1
        try:
            run_once(client, config, strategy, cycle)
        except Exception:
            log.exception("CYCLE %d FAILED", cycle)
        time.sleep(config.poll_seconds)
