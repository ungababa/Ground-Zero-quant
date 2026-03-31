import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import GridConfig, PairRules
from src.portfolio_runtime import pending_orders
from main import load_environment, parse_args, run_once
from src.strategy import GridStrategy


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
            "SpotWallet": {
                "ETH": {"Free": 20.0, "Lock": 0.0},
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
    def test_parse_args_accepts_multiple_env_files(self) -> None:
        args = parse_args(["--env", ".env.sol", "--env", ".env.btc"])
        self.assertEqual(args.env_files, [".env.sol", ".env.btc"])

    @patch("main.load_dotenv")
    def test_load_environment_loads_default_env_when_not_provided(self, mock_load_dotenv) -> None:
        load_environment([])
        mock_load_dotenv.assert_called_once_with()

    @patch("main.load_dotenv")
    def test_load_environment_loads_multiple_files_in_order(self, mock_load_dotenv) -> None:
        mock_load_dotenv.return_value = True
        load_environment([".env.sol", ".env.btc"])
        mock_load_dotenv.assert_has_calls(
            [
                call(dotenv_path=".env.sol", override=True),
                call(dotenv_path=".env.btc", override=True),
            ]
        )

    def test_run_once_places_orders_from_mock_market_data(self) -> None:
        config = GridConfig(
            pair="BTC/USD",
            levels_per_side=2,
            spacing_pct=0.01,
            per_level_notional_usd=1000,
            max_position_notional_usd=3000,
            max_open_orders=4,
            target_weight_pct=0.5,
            order_size_pct_of_target=0.05,
            adaptive_order_levels=2,
            min_inventory_floor_pct_of_target=0.0,
            enable_signal_tilt=False,
            reanchor_after_fill=False,
        )
        rules = PairRules(pair="BTC/USD", price_precision=2, amount_precision=6, min_order_value=1.0)
        strategy = GridStrategy(config, rules)
        client = FakeClient()

        result = run_once(client, config, strategy, cycle=1)

        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["placed_orders"], 0)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(len(client.placed), result["placed_orders"])

    def test_run_once_pauses_on_large_market_move(self) -> None:
        config = GridConfig(pair="BTC/USD", max_24h_abs_change_pct=0.03)
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

    def test_pending_orders_returns_empty_when_api_fails(self) -> None:
        self.assertEqual(
            pending_orders({"Success": False, "ErrMsg": "no order matched", "OrderMatched": []}),
            [],
        )


if __name__ == "__main__":
    unittest.main()