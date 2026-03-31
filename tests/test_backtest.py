import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.backtest import load_history, run_backtest
from src.config import GridConfig


class BacktestTests(unittest.TestCase):
    def test_run_backtest_with_mock_history(self) -> None:
        history = pd.DataFrame(
            [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": 100.0,
                    "high": 101.5,
                    "low": 98.5,
                    "close": 100.0,
                },
                {
                    "timestamp": "2026-01-02T00:00:00Z",
                    "open": 100.0,
                    "high": 102.5,
                    "low": 97.5,
                    "close": 100.0,
                },
                {
                    "timestamp": "2026-01-03T00:00:00Z",
                    "open": 100.0,
                    "high": 103.0,
                    "low": 97.0,
                    "close": 100.0,
                },
            ]
        )
        history["timestamp"] = pd.to_datetime(history["timestamp"], utc=True)
        config = GridConfig(
            levels_per_side=1,
            spacing_pct=0.01,
            per_level_notional_usd=1000,
            max_position_notional_usd=2000,
            enable_signal_tilt=False,
        )
        equity_curve, trades = run_backtest(config, history)
        self.assertEqual(len(equity_curve), 3)
        self.assertGreaterEqual(len(trades), 1)
        self.assertIn("equity", equity_curve.columns)

    def test_load_history_from_yfinance_like_frame(self) -> None:
        fake_yf = types.SimpleNamespace(
            download=lambda *args, **kwargs: pd.DataFrame(
                {
                    "Open": [100.0, 101.0],
                    "High": [102.0, 103.0],
                    "Low": [99.0, 100.0],
                    "Close": [101.0, 102.0],
                },
                index=pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
            )
        )
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            df = load_history(days=2, interval="1d")
        self.assertEqual(list(df.columns), ["timestamp", "open", "high", "low", "close"])
        self.assertEqual(len(df), 2)

    def test_load_history_raises_when_yfinance_returns_empty(self) -> None:
        fake_yf = types.SimpleNamespace(download=lambda *args, **kwargs: pd.DataFrame())
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            with self.assertRaisesRegex(ValueError, "No historical data returned from yfinance"):
                load_history(days=2, interval="1h")


if __name__ == "__main__":
    unittest.main()