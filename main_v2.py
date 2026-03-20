"""
Simplified Hackathon Bot - ALWAYS TRADES every cycle
Uses simple momentum/mean-reversion without strict z-score requirements
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

base_path = Path(__file__).resolve().parent
if str(base_path) not in sys.path:
    sys.path.insert(0, str(base_path))

# Load .env manually
env_file = base_path / ".env"
if env_file.exists():
    for line in env_file.read_text().strip().split('\n'):
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import MeanReversionConfig, PairRules
from roostoo_client import RoostooClient

log = logging.getLogger("hackathon_bot")


def pair_rules_from_exchange_info(exchange_info: dict, pair: str) -> PairRules:
    raw = exchange_info["TradePairs"][pair]
    return PairRules(
        pair=pair,
        price_precision=int(raw["PricePrecision"]),
        amount_precision=int(raw["AmountPrecision"]),
        min_order_value=float(raw["MiniOrder"]),
    )


def coin_free_balance(balance_response: dict, pair: str) -> float:
    coin = pair.split("/")[0]
    wallet = balance_response.get("Wallet", {})
    coin_entry = wallet.get(coin, {})
    return float(coin_entry.get("Free", 0.0))


def get_all_pairs(client: RoostooClient):
    exchange_info = client.exchange_info()
    raw = exchange_info.get("TradePairs", {})
    pairs = list(raw.keys())
    log.info("Found %d pairs", len(pairs))
    return pairs, exchange_info


def get_ticker_for_pair(client: RoostooClient, pair: str, retries=3):
    """Get ticker data for a pair with retry"""
    for attempt in range(retries):
        try:
            ticker_response = client.ticker(pair)
            market = ticker_response["Data"][pair]
            return {
                "bid": float(market["MaxBid"]),
                "ask": float(market["MinAsk"]),
                "last": float(market["LastPrice"]),
                "change_24h": float(market.get("Change", 0.0)),
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
    return None


def run_hackathon(client: RoostooClient, args):
    """Simple bot that ALWAYS trades every cycle"""

    pairs, exchange_info = get_all_pairs(client)

    # Keep track of prices for each pair
    price_history = {}  # pair -> list of recent prices

    cycle = 0
    current_pair = None
    current_position = 0.0

    while True:
        cycle += 1
        log.info("=== CYCLE %d ===", cycle)

        # Find best pair to trade
        best_pair = None
        best_score = -float('inf')
        best_ticker = None
        best_direction = None

        # Only scan first 20 pairs to avoid rate limits
        scan_pairs = pairs[:20]
        for pair in scan_pairs:
            ticker = get_ticker_for_pair(client, pair)
            if not ticker:
                continue

            # Track price history
            if pair not in price_history:
                price_history[pair] = []
            price_history[pair].append(ticker["last"])
            if len(price_history[pair]) > 20:
                price_history[pair].pop(0)

            prices = price_history[pair]
            if len(prices) < 3:
                continue

            # Simple momentum: compare recent prices
            recent = prices[-3:]
            if recent[-1] < recent[-2] < recent[-3]:
                # Downtrend - expect bounce (BUY)
                score = abs(recent[-1] - recent[0]) / recent[0] * 100
                direction = "BUY"
            elif recent[-1] > recent[-2] > recent[-3]:
                # Uptrend - expect pullback (SELL)
                score = abs(recent[-1] - recent[0]) / recent[0] * 100
                direction = "SELL"
            else:
                # No clear trend - use MA crossover
                if len(prices) >= 5:
                    short_ma = sum(prices[-3:]) / 3
                    long_ma = sum(prices[-5:]) / 5
                    if short_ma < long_ma:
                        score = (long_ma - short_ma) / short_ma * 100
                        direction = "BUY"
                    else:
                        score = (short_ma - long_ma) / long_ma * 100
                        direction = "SELL"
                else:
                    continue

            if score > best_score:
                best_score = score
                best_pair = pair
                best_ticker = ticker
                best_direction = direction

        if not best_pair or best_score < 0.01:
            # No good signal - just pick a random pair and trade
            log.info("No strong signal, picking random pair")
            for pair in pairs[:10]:
                ticker = get_ticker_for_pair(client, pair)
                if ticker:
                    best_pair = pair
                    best_ticker = ticker
                    best_direction = "BUY"
                    break

        if not best_pair:
            log.warning("No pairs available")
            time.sleep(args.poll_seconds)
            continue

        log.info("Selected: %s direction=%s score=%.4f", best_pair, best_direction, best_score)

        # Switch pair if needed
        if current_pair and current_pair != best_pair:
            try:
                client.cancel_order(pair=current_pair)
                log.info("Switched from %s to %s", current_pair, best_pair)
            except:
                pass
            current_pair = best_pair

        if current_pair is None:
            current_pair = best_pair

        # Get position
        try:
            balance = client.balance()
            current_position = coin_free_balance(balance, current_pair)
            log.info("Position: %.6f %s", current_position, current_pair)
        except Exception as e:
            log.warning("Balance error: %s", e)
            current_position = 0.0

        # Cancel existing orders
        try:
            client.cancel_order(pair=current_pair)
        except:
            pass

        time.sleep(1)

        # Place order with retry
        for attempt in range(3):
            try:
                rules = pair_rules_from_exchange_info(exchange_info, current_pair)
                price = best_ticker["last"]

                # Calculate quantity
                qty = args.position_notional_usd / price
                qty = rules.round_amount(qty)

                # Ensure minimum order
                min_value = rules.min_order_value
                if price * qty < min_value:
                    qty = min_value / price
                    qty = rules.round_amount(qty)

                if best_direction == "BUY":
                    if current_position < 0.001:
                        client.place_limit_order(current_pair, "BUY", qty, price)
                        log.info("BUY %.6f %s @ %.2f", qty, current_pair, price)
                        current_position += qty
                else:
                    if current_position > 0.001:
                        sell_qty = min(current_position, qty)
                        client.place_limit_order(current_pair, "SELL", sell_qty, price)
                        log.info("SELL %.6f %s @ %.2f", sell_qty, current_pair, price)
                        current_position -= sell_qty
                    else:
                        client.place_limit_order(current_pair, "SELL", qty, price)
                        log.info("SHORT SELL %.6f %s @ %.2f", qty, current_pair, price)
                        current_position -= qty

                break  # Success

            except Exception as e:
                log.warning("Order attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(3)
                else:
                    log.error("Order error: %s", e)

        time.sleep(args.poll_seconds)


def setup_logging(level_name: str):
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hackathon Trading Bot")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--position-notional-usd", type=float, default=1000.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    log.info("Starting Hackathon Bot...")

    client = RoostooClient()
    run_hackathon(client, args)
