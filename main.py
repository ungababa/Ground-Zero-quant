from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from dotenv import load_dotenv

from config import GridConfig, PairRules
from roostoo_client import RoostooClient
from strategy import GridStrategy, TickerView

log = logging.getLogger("main")


def pair_rules_from_exchange_info(exchange_info: dict, pair: str) -> PairRules:
    raw = exchange_info["TradePairs"][pair]
    rules = PairRules(
        pair=pair,
        price_precision=int(raw["PricePrecision"]),
        amount_precision=int(raw["AmountPrecision"]),
        min_order_value=float(raw["MiniOrder"]),
    )
    log.debug(
        "PairRules parsed | pair=%s  price_prec=%d  amount_prec=%d  min_order_value=%.4f",
        rules.pair,
        rules.price_precision,
        rules.amount_precision,
        rules.min_order_value,
    )
    return rules


def coin_free_balance(balance_response: dict, pair: str) -> float:
    if not balance_response.get("Success", True):
        log.warning("balance returned Success=False")
        return 0.0
    coin = pair.split("/")[0]
    wallet = balance_response.get("SpotWallet", {})
    if not wallet:
        log.warning("SpotWallet missing from balance response: %s", balance_response)
    coin_entry = wallet.get(coin, {})
    return float(coin_entry.get("Free", 0.0))


def usd_free_balance(balance_response: dict) -> float:
    wallet = balance_response.get("SpotWallet", {})
    if not wallet:
        log.warning("SpotWallet missing from balance response: %s", balance_response)
    usd_entry = wallet.get("USD", {})
    return float(usd_entry.get("Free", 0.0))


def pending_orders(order_response: dict) -> list[dict]:
    success = order_response.get("Success")
    all_orders = order_response.get("OrderMatched", [])
    if not success:
        log.warning("query_orders returned Success=False, treating as 0 pending orders")
        return []
    pending = [o for o in all_orders if o.get("Status") == "PENDING"]
    return pending


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default=None)
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument(
        "--env",
        dest="env_files",
        action="append",
        default=[],
        help="Path to an env file. Can be passed multiple times; later files override earlier ones.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def load_environment(env_files: list[str]) -> None:
    if not env_files:
        load_dotenv()
        return

    for env_file in env_files:
        loaded = load_dotenv(dotenv_path=env_file, override=True)
        if loaded:
            log.info("Loaded env file: %s", env_file)
        else:
            log.warning("Env file not found or empty: %s", env_file)


def reserve_cash_usd(config: GridConfig) -> float:
    if config.reserve_cash_usd > 0:
        return config.reserve_cash_usd
    if config.capital_base_usd > 0:
        return config.capital_base_usd * config.cash_reserve_pct
    return 0.0


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

    balance = client.balance()
    log.debug("BALANCE RESPONSE: %s", json.dumps(balance, default=str))

    coin_position = coin_free_balance(balance, config.pair)
    usd_free = usd_free_balance(balance)

    reserve_usd = reserve_cash_usd(config)

    # Shared-wallet-safe budgeting:
    # each bot is capped by its own capital sleeve, not the full wallet USD.
    bot_inventory_value = coin_position * ticker.mid
    wallet_deployable_cash = max(usd_free - reserve_usd, 0.0)

    if config.capital_base_usd > 0:
        bot_budget_remaining = max(config.capital_base_usd - bot_inventory_value - reserve_usd, 0.0)
    else:
        bot_budget_remaining = wallet_deployable_cash

    deployable_cash = min(wallet_deployable_cash, bot_budget_remaining)

    buy_locked = deployable_cash <= 0.0
    sell_locked = coin_position <= 0.0

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
            ):
                refresh_ok = False

        if refresh_ok:
            log.info("REFRESH triggered — cancelling all orders and re-anchoring to mid=%.2f", ticker.mid)
            cancel_resp = client.cancel_order(pair=config.pair)
            log.debug("Cancel-all response: %s", json.dumps(cancel_resp, default=str))
            strategy.set_anchor(ticker.mid)
            refresh_triggered = True
        else:
            log.info(
                "Refresh blocked due to inventory lock | buy_locked=%s sell_locked=%s anchor=%.2f mid=%.2f",
                buy_locked,
                sell_locked,
                strategy.anchor_price,
                ticker.mid,
            )

    orders_resp = client.query_orders(config.pair, pending_only=True, limit=config.max_open_orders)
    log.debug("QUERY_ORDERS RESPONSE: %s", json.dumps(orders_resp, default=str))
    open_orders = pending_orders(orders_resp)

    desired = strategy.desired_orders(
        ticker=ticker,
        coin_position=coin_position,
        usd_free=deployable_cash,
    )

    placed = 0

    desired_map = {(o.side, round(o.price, 8), round(o.quantity, 8)): o for o in desired}
    open_map = {
        (
            order.get("Side"),
            round(float(order.get("Price", 0.0)), 8),
            round(float(order.get("Quantity", 0.0)), 8),
        ): order
        for order in open_orders
    }

    to_cancel = [order for key, order in open_map.items() if key not in desired_map]
    to_place = [order for key, order in desired_map.items() if key not in open_map]

    if to_cancel:
        log.info("Cancelling %d stale orders", len(to_cancel))
        for order in to_cancel:
            try:
                client.cancel_order(order_id=str(order["OrderID"]))
            except Exception as exc:
                log.exception("cancel_order failed | order_id=%s err=%s", order.get("OrderID"), exc)

    if to_place:
        log.info("Placing %d orders", min(len(to_place), config.max_open_orders))
        for i, order in enumerate(to_place[: config.max_open_orders]):
            log.info("  [%d] %s", i, order)
            resp = client.place_limit_order(config.pair, order.side, order.quantity, order.price)
            log.debug("  place_order response: %s", json.dumps(resp, default=str))
            placed += 1

    result = {
        "status": "ok",
        "mid": ticker.mid,
        "coin_free": coin_position,
        "usd_free": usd_free,
        "reserve_usd": reserve_usd,
        "wallet_deployable_cash": wallet_deployable_cash,
        "bot_inventory_value": bot_inventory_value,
        "bot_budget_remaining": bot_budget_remaining,
        "deployable_cash": deployable_cash,
        "buy_locked": buy_locked,
        "sell_locked": sell_locked,
        "refresh_triggered": refresh_triggered,
        "anchor": strategy.anchor_price,
        "desired_orders": len(desired),
        "placed_orders": placed,
    }
    log.info("CYCLE %d RESULT: %s", cycle, json.dumps(result, indent=2))
    return result


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    fmt = "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(name)s]  %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
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

    log.info("Config: %s", json.dumps(asdict(config), indent=2, default=str))

    client = RoostooClient()

    log.info("Fetching exchange info ...")
    exchange_info = client.exchange_info()
    rules = pair_rules_from_exchange_info(exchange_info, config.pair)

    strategy = GridStrategy(config, rules)

    cycle = 0
    while True:
        cycle += 1
        try:
            run_once(client, config, strategy, cycle)
        except Exception:
            log.exception("CYCLE %d FAILED with exception", cycle)
        log.info("Sleeping %d seconds until next cycle ...", config.poll_seconds)
        time.sleep(config.poll_seconds)