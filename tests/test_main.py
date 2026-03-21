import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GridConfig, PairRules
from main import pending_orders, run_once
from strategy import GridStrategy


class FakeClient:
    def __init__(self, change: float = 0.0) -> None:
        self.cancel_calls = 0
        self.placed = []
        self.change = change

    def ticker(self, pair: str) -> dict:
        return {
            "Data": {
                pair: {
                    "MaxBid": 100.0,
                    "MinAsk": 100.0,
                    "LastPrice": 100.0,
                    "Change": self.change,
                }
            }
        }

    def balance(self) -> dict:
        return {
            "Wallet": {
                "BTC": {"Free": 0.0, "Lock": 0.0},
                "USD": {"Free": 50000.0, "Lock": 0.0},
            }
        }

    def cancel_order(self, order_id=None, pair=None) -> dict:
        self.cancel_calls += 1
        return {"Success": True, "CanceledList": []}

    def query_orders(self, pair: str, pending_only: bool = False, limit: int = 100) -> dict:
        return {"Success": False, "ErrMsg": "no order matched", "OrderMatched": []}

    def place_limit_order(self, pair: str, side: str, quantity: float, price: float) -> dict:
        self.placed.append({"pair": pair, "side": side, "quantity": quantity, "price": price})
        return {"Success": True}


class MainTests(unittest.TestCase):
    def test_run_once_places_orders_from_mock_market_data(self) -> None:
        config = GridConfig(
            pair="BTC/USD",
            levels_per_side=2,
            spacing_pct=0.01,
            per_level_notional_usd=1000,
            max_position_notional_usd=3000,
            max_open_orders=4,
        )
        rules = PairRules(pair="BTC/USD", price_precision=2, amount_precision=6, min_order_value=1.0)
        strategy = GridStrategy(config, rules)
        client = FakeClient()

        result = run_once(client, config, strategy, cycle=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["placed_orders"], 2)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(len(client.placed), 2)

    def test_run_once_pauses_on_large_market_move(self) -> None:
        config = GridConfig(max_24h_abs_change_pct=0.03)
        rules = PairRules(pair="BTC/USD", price_precision=2, amount_precision=6, min_order_value=1.0)
        strategy = GridStrategy(config, rules)
        client = FakeClient(change=0.05)

        result = run_once(client, config, strategy, cycle=1)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(client.cancel_calls, 0)
        self.assertEqual(len(client.placed), 0)

    def test_pending_orders_filters_non_pending_statuses(self) -> None:
        filtered = pending_orders(
            {
                "Success": True,
                "OrderMatched": [
                    {"Status": "PENDING", "OrderID": 1},
                    {"Status": "FILLED", "OrderID": 2},
                    {"Status": "CANCELED", "OrderID": 3},
                ],
            }
        )
        self.assertEqual(filtered, [{"Status": "PENDING", "OrderID": 1}])


if __name__ == "__main__":
    unittest.main()
