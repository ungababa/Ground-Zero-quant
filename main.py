import argparse
import time

from config import GridConfig, PairRules
from dotenv import load_dotenv
from roostoo_client import RoostooClient
from strategy import GridStrategy, TickerView


def pair_rules_from_exchange_info(exchange_info: dict, pair: str) -> PairRules:
    raw = exchange_info["TradePairs"][pair]
    return PairRules(
        pair=pair,
        price_precision=int(raw["PricePrecision"]),
        amount_precision=int(raw["AmountPrecision"]),
        min_order_value=float(raw["MiniOrder"]),
    )


def btc_free_balance(balance_response: dict) -> float:
    wallet = balance_response.get("Wallet", {})
    return float(wallet.get("BTC", {}).get("Free", 0.0))


def pending_orders(order_response: dict) -> list[dict]:
    if not order_response.get("Success"):
        return []
    return [
        order
        for order in order_response.get("OrderMatched", [])
        if order.get("Status") == "PENDING"
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="BTC/USD")
    parser.add_argument("--poll-seconds", type=int, default=None)
    return parser.parse_args()


def run_once(client: RoostooClient, config: GridConfig, strategy: GridStrategy) -> dict:
    ticker_response = client.ticker(config.pair)
    market = ticker_response["Data"][config.pair]
    ticker = TickerView(
        bid=float(market["MaxBid"]),
        ask=float(market["MinAsk"]),
        last=float(market["LastPrice"]),
        change_24h=float(market["Change"]),
    )
    if strategy.should_pause(ticker):
        return {
            "status": "paused",
            "reason": "trend_filter",
            "change_24h": ticker.change_24h,
        }

    balance = client.balance()
    btc_position = btc_free_balance(balance)

    if strategy.should_refresh(ticker):
        client.cancel_order(pair=config.pair)
        strategy.set_anchor(ticker.mid)

    open_orders = pending_orders(
        client.query_orders(
            config.pair, pending_only=True, limit=config.max_open_orders
        )
    )
    desired = strategy.desired_orders(ticker, btc_position)
    placed = 0

    if not strategy.equivalent(open_orders, desired):
        if open_orders:
            client.cancel_order(pair=config.pair)
        for order in desired[: config.max_open_orders]:
            client.place_limit_order(
                config.pair, order.side, order.quantity, order.price
            )
            placed += 1

    return {
        "status": "ok",
        "mid": ticker.mid,
        "btc_free": btc_position,
        "anchor": strategy.anchor_price,
        "desired_orders": len(desired),
        "placed_orders": placed,
    }


if __name__ == "__main__":
    load_dotenv()
    args = parse_args()
    config = GridConfig.from_env()
    config.pair = args.pair
    if args.poll_seconds is not None:
        config.poll_seconds = args.poll_seconds

    client = RoostooClient()
    rules = pair_rules_from_exchange_info(client.exchange_info(), config.pair)
    strategy = GridStrategy(config, rules)

    while True:
        print(run_once(client, config, strategy))
        time.sleep(config.poll_seconds)
