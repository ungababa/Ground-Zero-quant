import argparse
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from config import GridConfig, PairRules
from dotenv import load_dotenv
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
    coin = pair.split("/")[0]
    wallet = balance_response.get("Wallet", {})
    log.debug("Full wallet snapshot: %s", json.dumps(wallet, indent=2, default=str))
    coin_entry = wallet.get(coin, {})
    if not coin_entry:
        log.warning("Coin %s not found in wallet — available keys: %s", coin, list(wallet.keys()))
    free = float(coin_entry.get("Free", 0.0))
    locked = coin_entry.get("Locked", "N/A")
    total = coin_entry.get("Total", "N/A")
    log.debug(
        "%s wallet | Free=%.8f  Locked=%s  Total=%s",
        coin,
        free,
        locked,
        total,
    )
    usd_entry = wallet.get("USD", {})
    log.debug(
        "USD wallet | Free=%s  Locked=%s  Total=%s",
        usd_entry.get("Free", "N/A"),
        usd_entry.get("Locked", "N/A"),
        usd_entry.get("Total", "N/A"),
    )
    return free


def pending_orders(order_response: dict) -> list[dict]:
    success = order_response.get("Success")
    all_orders = order_response.get("OrderMatched", [])
    log.debug(
        "query_orders response | Success=%s  total_orders_returned=%d",
        success,
        len(all_orders),
    )
    if not success:
        log.warning("query_orders returned Success=False, treating as 0 pending orders")
        return []
    pending = [o for o in all_orders if o.get("Status") == "PENDING"]
    log.debug("Pending orders (%d):", len(pending))
    for i, o in enumerate(pending):
        log.debug(
            "  [%d] id=%s  side=%s  price=%s  qty=%s  status=%s  pair=%s",
            i,
            o.get("OrderId", "?"),
            o.get("Side", "?"),
            o.get("Price", "?"),
            o.get("Quantity", "?"),
            o.get("Status", "?"),
            o.get("Pair", "?"),
        )
    non_pending = [o for o in all_orders if o.get("Status") != "PENDING"]
    if non_pending:
        log.debug("Non-pending orders (%d):", len(non_pending))
        for o in non_pending:
            log.debug(
                "  id=%s  side=%s  price=%s  qty=%s  status=%s",
                o.get("OrderId", "?"),
                o.get("Side", "?"),
                o.get("Price", "?"),
                o.get("Quantity", "?"),
                o.get("Status", "?"),
            )
    return pending


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="SOL/USD")
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def run_once(
    client: RoostooClient, config: GridConfig, strategy: GridStrategy, cycle: int
) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    log.info("=" * 80)
    log.info("CYCLE %d  started at %s", cycle, ts)
    log.info("=" * 80)

    log.debug(
        "Strategy state | anchor_price=%s  pair=%s",
        strategy.anchor_price,
        config.pair,
    )

    # --- Ticker ---
    log.debug("Fetching ticker for %s ...", config.pair)
    ticker_response = client.ticker(config.pair)
    market = ticker_response["Data"][config.pair]
    log.debug("Raw market data: %s", json.dumps(market, indent=2, default=str))
    ticker = TickerView(
        bid=float(market["MaxBid"]),
        ask=float(market["MinAsk"]),
        last=float(market["LastPrice"]),
        change_24h=float(market["Change"]),
    )
    log.info("TICKER  %s", ticker)

    # --- Pause check ---
    if strategy.should_pause(ticker):
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

    # --- Balance ---
    coin = config.pair.split("/")[0]
    log.debug("Fetching balance ...")
    balance = client.balance()
    coin_position = coin_free_balance(balance, config.pair)
    log.info(
        "%s FREE BALANCE  %.8f  (~$%.2f at mid)",
        coin,
        coin_position,
        coin_position * ticker.mid,
    )

    # --- Refresh check ---
    if strategy.should_refresh(ticker):
        log.info("REFRESH triggered — cancelling all orders and re-anchoring to mid=%.2f", ticker.mid)
        cancel_resp = client.cancel_order(pair=config.pair)
        log.debug("Cancel-all response: %s", json.dumps(cancel_resp, default=str))
        strategy.set_anchor(ticker.mid)

    # --- Open orders ---
    log.debug("Querying open orders (pending_only=True, limit=%d) ...", config.max_open_orders)
    open_orders = pending_orders(
        client.query_orders(
            config.pair, pending_only=True, limit=config.max_open_orders
        )
    )

    # --- Desired orders ---
    desired = strategy.desired_orders(ticker, coin_position)
    placed = 0

    # --- Equivalence check & placement ---
    orders_match = strategy.equivalent(open_orders, desired)
    if orders_match:
        log.info("Orders on book already match desired grid — no action needed")
    else:
        log.info(
            "Orders MISMATCH — live=%d  desired=%d — will cancel and re-place",
            len(open_orders),
            len(desired),
        )
        if open_orders:
            cancel_resp = client.cancel_order(pair=config.pair)
            log.debug("Cancel-all response: %s", json.dumps(cancel_resp, default=str))

        to_place = desired[: config.max_open_orders]
        log.info("Placing %d orders (max_open_orders=%d):", len(to_place), config.max_open_orders)
        for i, order in enumerate(to_place):
            log.info("  [%d] %s", i, order)
            resp = client.place_limit_order(
                config.pair, order.side, order.quantity, order.price
            )
            log.debug("  place_order response: %s", json.dumps(resp, default=str))
            placed += 1

    result = {
        "status": "ok",
        "mid": ticker.mid,
        "coin": coin,
        "coin_free": coin_position,
        "anchor": strategy.anchor_price,
        "desired_orders": len(desired),
        "placed_orders": placed,
    }
    log.info("CYCLE %d RESULT: %s", cycle, json.dumps(result, indent=2))
    return result


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.DEBUG)
    fmt = (
        "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(name)s]  %(message)s"
    )
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )


if __name__ == "__main__":
    load_dotenv()
    args = parse_args()

    setup_logging(args.log_level)

    config = GridConfig.from_env()
    config.pair = args.pair
    if args.poll_seconds is not None:
        config.poll_seconds = args.poll_seconds

    log.info("=" * 80)
    log.info("GRID TRADING BOT — STARTUP")
    log.info("=" * 80)
    log.info("Config: %s", json.dumps(asdict(config), indent=2, default=str))

    client = RoostooClient()

    log.info("Fetching exchange info ...")
    exchange_info = client.exchange_info()
    rules = pair_rules_from_exchange_info(exchange_info, config.pair)
    log.info(
        "PairRules ready: pair=%s  price_prec=%d  amount_prec=%d  min_order=%.4f",
        rules.pair,
        rules.price_precision,
        rules.amount_precision,
        rules.min_order_value,
    )

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
