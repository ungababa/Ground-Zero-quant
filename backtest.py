from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from config import GridConfig, PairRules
from metrics import summarize_equity_curve
from strategy import GridStrategy, TickerView

FEE_RATE = 0.05 / 100


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [column[0] if isinstance(column, tuple) else column for column in df.columns]

    df = df.reset_index().rename(
        columns={
            "index": "timestamp",
            "Datetime": "timestamp",
            "Date": "timestamp",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
        }
    )

    if "timestamp" not in df.columns:
        df = df.rename(columns={df.columns[0]: "timestamp"})

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="raise")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    return (
        df[["timestamp", "open", "high", "low", "close"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def load_history(
    days: int,
    csv_path: str | None = None,
    interval: str = "1h",
    ticker: str = "SOL-USD",
) -> pd.DataFrame:
    if csv_path:
        df = pd.read_csv(csv_path)
        return _normalize_history(df)

    import yfinance as yf

    df = yf.download(
        ticker,
        period=f"{days}d",
        interval=interval,
        auto_adjust=False,
        progress=False,
    )

    if df.empty:
        raise ValueError(
            f"No historical data returned from yfinance for {ticker}. "
            "Provide --csv with explicit OHLC data or investigate Yahoo/yfinance availability."
        )

    return _normalize_history(df)


def _orders_to_json(orders: list) -> str:
    return json.dumps(
        [{"side": order.side, "price": order.price, "quantity": order.quantity} for order in orders],
        separators=(",", ":"),
    )


def _update_average_cost(
    avg_cost: float,
    current_coin: float,
    buy_qty: float,
    buy_price: float,
    buy_fee: float,
) -> float:
    effective_buy_cost = buy_price + (buy_fee / buy_qty)
    if current_coin <= 0:
        return effective_buy_cost
    return ((avg_cost * current_coin) + (effective_buy_cost * buy_qty)) / (current_coin + buy_qty)


def _compute_change_24h(df: pd.DataFrame) -> np.ndarray:
    ts = df["timestamp"].values.astype("datetime64[ns]")
    closes = df["close"].to_numpy(dtype=float)
    window = np.timedelta64(24, "h")
    changes = np.full(len(df), np.nan)

    for i in range(len(df)):
        cutoff = ts[i] - window
        j = int(np.searchsorted(ts, cutoff, side="right")) - 1
        if j >= 0:
            changes[i] = (closes[i] - closes[j]) / closes[j]

    return changes


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]

    if isinstance(obj, np.generic):
        obj = obj.item()

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None

    return obj


def simulate_backtest(
    config: GridConfig,
    history: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    rules = PairRules(pair=config.pair, price_precision=2, amount_precision=6, min_order_value=1.0)
    strategy = GridStrategy(config, rules)

    initial_price = float(history.iloc[0].open)
    starting_cash = 1_000_000.0
    target_notional = starting_cash * 0.5
    initial_qty = target_notional / initial_price
    initial_fee = target_notional * FEE_RATE

    cash = starting_cash - target_notional - initial_fee
    coin = initial_qty
    avg_cost = initial_price
    realized_pnl = 0.0

    reserve_cash_abs = starting_cash * config.cash_reserve_pct

    trades: list[dict] = [
        {
            "timestamp": history.iloc[0].timestamp,
            "side": "BUY",
            "price": initial_price,
            "quantity": initial_qty,
            "fee": initial_fee,
        }
    ]

    equity_rows: list[dict] = []
    detail_rows: list[dict] = []

    change_24h_arr = _compute_change_24h(history)
    change_24h_arr = np.concatenate([[np.nan], change_24h_arr[:-1]])

    for bar_idx, row in enumerate(history.itertuples(index=False)):
        open_price = float(row.open)
        close = float(row.close)
        low = float(row.low)
        high = float(row.high)

        raw_c24h = change_24h_arr[bar_idx]
        c24h = float(raw_c24h) if not np.isnan(raw_c24h) else 0.0

        tick = TickerView(
            bid=open_price,
            ask=open_price,
            last=open_price,
            change_24h=c24h,
        )

        strategy.record_price(tick.mid)

        deployable_cash = max(cash - reserve_cash_abs, 0.0)
        buy_locked = deployable_cash <= 0.0
        sell_locked = coin <= 0.0

        paused = config.pause_guard and strategy.should_pause(tick)

        refresh_triggered = False
        if not paused and (strategy.anchor_price is None or strategy.should_refresh(tick)):
            refresh_ok = True

            if strategy.anchor_price is not None:
                if (
                    config.disable_downward_refresh_when_no_cash
                    and buy_locked
                    and tick.mid < strategy.anchor_price
                ):
                    refresh_ok = False

                if (
                    config.disable_upward_refresh_when_no_coin
                    and sell_locked
                    and tick.mid > strategy.anchor_price
                ):
                    refresh_ok = False

            if refresh_ok:
                refresh_triggered = True
                strategy.set_anchor(tick.mid)

        fills_this_bar: list[dict] = []
        desired: list = []

        if not paused:
            desired = strategy.desired_orders(
                tick,
                coin_position=coin,
                usd_free=deployable_cash,
            )

        for order in desired[: config.max_open_orders]:
            filled = (
                (order.side == "BUY" and low <= order.price)
                or (order.side == "SELL" and high >= order.price)
            )
            if not filled:
                continue

            notional = order.price * order.quantity
            fee = notional * FEE_RATE

            if order.side == "BUY" and cash >= notional + fee:
                avg_cost = _update_average_cost(avg_cost, coin, order.quantity, order.price, fee)
                cash -= notional + fee
                coin += order.quantity

                fill = {
                    "timestamp": row.timestamp,
                    "side": order.side,
                    "price": order.price,
                    "quantity": order.quantity,
                    "fee": fee,
                }
                trades.append(fill)
                fills_this_bar.append(fill)

                if config.reanchor_after_fill:
                    strategy.set_anchor(order.price)

            elif order.side == "SELL" and coin >= order.quantity:
                realized_pnl += (order.price - avg_cost) * order.quantity - fee
                cash += notional - fee
                coin -= order.quantity

                if coin == 0:
                    avg_cost = 0.0

                fill = {
                    "timestamp": row.timestamp,
                    "side": order.side,
                    "price": order.price,
                    "quantity": order.quantity,
                    "fee": fee,
                }
                trades.append(fill)
                fills_this_bar.append(fill)

                if config.reanchor_after_fill:
                    strategy.set_anchor(order.price)

        equity = cash + coin * close
        unrealized_pnl = coin * (close - avg_cost) if coin > 0 else 0.0

        next_deployable_cash = max(cash - reserve_cash_abs, 0.0)
        next_orders = [] if paused else strategy.desired_orders(
            tick,
            coin_position=coin,
            usd_free=next_deployable_cash,
        )

        bh_equity = (starting_cash / initial_price) * close

        equity_rows.append(
            {
                "timestamp": row.timestamp,
                "equity": equity,
                "bh_equity": bh_equity,
                "cash": cash,
                "coin": coin,
                "close": close,
            }
        )

        detail_rows.append(
            {
                "timestamp": row.timestamp,
                "open": float(row.open),
                "high": high,
                "low": low,
                "close": close,
                "anchor_price": strategy.anchor_price,
                "refresh_triggered": refresh_triggered,
                "current_position_coin": coin,
                "current_position_notional_usd": coin * close,
                "average_cost": avg_cost,
                "cash_usd": cash,
                "deployable_cash_usd": next_deployable_cash,
                "reserve_cash_usd": reserve_cash_abs,
                "coin_market_value_usd": coin * close,
                "equity_usd": equity,
                "realized_pnl_usd": realized_pnl,
                "unrealized_pnl_usd": unrealized_pnl,
                "current_limit_orders_json": _orders_to_json(next_orders),
                "fill_count": len(fills_this_bar),
                "fills_json": json.dumps(fills_this_bar, default=str, separators=(",", ":")),
                "paused": paused,
                "buy_locked": buy_locked,
                "sell_locked": sell_locked,
                "change_24h": c24h,
                "debug_max_position_notional_usd": config.max_position_notional_usd,
                "debug_spacing_pct": config.spacing_pct,
                "debug_buy_spacing_multiplier": config.buy_spacing_multiplier,
                "debug_sell_spacing_multiplier": config.sell_spacing_multiplier,
                "debug_nearest_buy_offset_multiplier": config.nearest_buy_offset_multiplier,
                "debug_nearest_sell_offset_multiplier": config.nearest_sell_offset_multiplier,
                "debug_levels_per_side": config.levels_per_side,
            }
        )

    return pd.DataFrame(equity_rows), trades, pd.DataFrame(detail_rows)


def run_backtest(config: GridConfig, history: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    equity_curve, trades, _ = simulate_backtest(config, history)
    return equity_curve, trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=str, default="SOL/USD", help="Trading pair, e.g. SOL/USD or BTC/USD")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--interval", type=str, default="1h")

    parser.add_argument("--spacing-pct", type=float, default=0.01)
    parser.add_argument("--buy-spacing-multiplier", type=float, default=1.0)
    parser.add_argument("--sell-spacing-multiplier", type=float, default=1.0)
    parser.add_argument("--nearest-buy-offset-multiplier", type=float, default=1.0)
    parser.add_argument("--nearest-sell-offset-multiplier", type=float, default=1.0)
    parser.add_argument("--levels-per-side", type=int, default=30)
    parser.add_argument("--per-level-notional-usd", type=float, default=10000)
    parser.add_argument(
        "--max-position-notional-usd",
        type=float,
        default=math.inf,
        help="Max notional USD of coin held. Defaults to unlimited.",
    )
    parser.add_argument("--refresh-threshold-pct", type=float, default=0.05)
    parser.add_argument("--max-open-orders", type=int, default=1000)
    parser.add_argument("--max-24h-abs-change-pct", type=float, default=0.05)

    parser.add_argument("--cash-reserve-pct", type=float, default=0.05)

    parser.add_argument(
        "--reanchor-after-fill",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--pause-guard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable the 24h-change volatility pause guard (default: enabled). "
        "Use --no-pause-guard to run the bot through all market conditions.",
    )
    parser.add_argument(
        "--disable-downward-refresh-when-no-cash",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--disable-upward-refresh-when-no-coin",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    yf_ticker = args.pair.replace("/", "-")

    config = GridConfig(
        pair=args.pair,
        spacing_pct=args.spacing_pct,
        buy_spacing_multiplier=args.buy_spacing_multiplier,
        sell_spacing_multiplier=args.sell_spacing_multiplier,
        nearest_buy_offset_multiplier=args.nearest_buy_offset_multiplier,
        nearest_sell_offset_multiplier=args.nearest_sell_offset_multiplier,
        levels_per_side=args.levels_per_side,
        per_level_notional_usd=args.per_level_notional_usd,
        max_position_notional_usd=args.max_position_notional_usd,
        refresh_threshold_pct=args.refresh_threshold_pct,
        max_open_orders=args.max_open_orders,
        max_24h_abs_change_pct=args.max_24h_abs_change_pct,
        reanchor_after_fill=args.reanchor_after_fill,
        pause_guard=args.pause_guard,
        cash_reserve_pct=args.cash_reserve_pct,
        disable_downward_refresh_when_no_cash=args.disable_downward_refresh_when_no_cash,
        disable_upward_refresh_when_no_coin=args.disable_upward_refresh_when_no_coin,
    )

    history = load_history(args.days, args.csv, args.interval, yf_ticker)
    equity_curve, trades, detail_log = simulate_backtest(config, history)
    summary = summarize_equity_curve(equity_curve)

    payload = {
        "config": asdict(config),
        "metrics": summary,
        "trades": len(trades),
    }

    print(json.dumps(_json_safe(payload)))

    out_dir = Path("artifacts")
    out_dir.mkdir(exist_ok=True)
    equity_curve.to_csv(out_dir / "equity_curve.csv", index=False)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    detail_log.to_csv(out_dir / "detailed_backtest_log.csv", index=False)