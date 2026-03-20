import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GridConfig, PairRules
from strategy import GridStrategy, TickerView


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = GridConfig(
            levels_per_side=2,
            spacing_pct=0.01,
            per_level_notional_usd=1000,
            max_position_notional_usd=3000,
        )
        self.rules = PairRules(pair="BTC/USD", price_precision=2, amount_precision=6, min_order_value=1.0)
        self.strategy = GridStrategy(self.config, self.rules)
        self.ticker = TickerView(bid=100.0, ask=100.0, last=100.0, change_24h=0.0)
        self.strategy.set_anchor(100.0)

    def test_desired_orders_builds_symmetric_grid(self) -> None:
        orders = self.strategy.desired_orders(self.ticker, coin_position=0.0)
        self.assertEqual(len(orders), 4)
        self.assertEqual([o.side for o in orders], ["BUY", "SELL", "BUY", "SELL"])
        self.assertEqual([o.price for o in orders], [99.0, 101.0, 98.0, 102.0])

    def test_pause_when_move_too_large(self) -> None:
        volatile = TickerView(bid=100.0, ask=100.0, last=100.0, change_24h=0.05)
        self.assertTrue(self.strategy.should_pause(volatile))

    def test_equivalent_matches_same_orders(self) -> None:
        desired = self.strategy.desired_orders(self.ticker, coin_position=0.0)
        live = [{"Side": o.side, "Price": o.price, "Quantity": o.quantity} for o in desired]
        self.assertTrue(self.strategy.equivalent(live, desired))

    def test_should_refresh_when_anchor_is_missing(self) -> None:
        self.strategy.anchor_price = None
        self.assertTrue(self.strategy.should_refresh(self.ticker))

    def test_min_order_value_filters_tiny_orders(self) -> None:
        strict_rules = PairRules(
            pair="BTC/USD",
            price_precision=2,
            amount_precision=6,
            min_order_value=5000.0,
        )
        strategy = GridStrategy(self.config, strict_rules)
        strategy.set_anchor(100.0)
        orders = strategy.desired_orders(self.ticker, coin_position=0.0)
        self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
