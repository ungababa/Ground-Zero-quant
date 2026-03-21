import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest import load_history, run_backtest, run_weighted_dual_backtest
from config import GridConfig


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
        )
        equity_curve, trades = run_backtest(config, history)
        self.assertEqual(len(equity_curve), 3)
        self.assertGreaterEqual(len(trades), 1)
        self.assertIn("equity", equity_curve.columns)

    @patch("backtest.yf.download")
    def test_load_history_from_yfinance_like_frame(self, mock_download) -> None:
        mock_download.return_value = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.0, 102.0],
            },
            index=pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
        )
        df = load_history(days=2, interval="1d")
        self.assertEqual(list(df.columns), ["timestamp", "open", "high", "low", "close"])
        self.assertEqual(len(df), 2)

    @patch("backtest.yf.download")
    def test_load_history_raises_when_yfinance_returns_empty(self, mock_download) -> None:
        mock_download.return_value = pd.DataFrame()
        with self.assertRaisesRegex(ValueError, "No historical data returned from yfinance"):
            load_history(days=2, interval="1h")

    def test_run_weighted_dual_backtest_tracks_per_asset_and_benchmark(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ],
            utc=True,
        )
        btc_history = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.0, 101.0, 102.0],
            }
        )
        eth_history = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [50.0, 49.0, 48.0],
                "high": [50.5, 49.5, 48.5],
                "low": [49.5, 48.5, 47.5],
                "close": [50.0, 49.0, 48.0],
            }
        )
        configs = {
            "BTC": GridConfig(pair="BTC/USD", levels_per_side=1, spacing_pct=0.01, per_level_notional_usd=1000),
            "ETH": GridConfig(pair="ETH/USD", levels_per_side=1, spacing_pct=0.01, per_level_notional_usd=1000),
        }

        equity_curve, trades = run_weighted_dual_backtest(
            configs=configs,
            histories={"BTC": btc_history, "ETH": eth_history},
            weights={"BTC": 0.2, "ETH": 0.8},
            invested_ratio=0.5,
        )

        self.assertEqual(len(equity_curve), 3)
        self.assertGreaterEqual(len(trades), 2)
        self.assertIn("equity", equity_curve.columns)
        self.assertIn("bh_equity", equity_curve.columns)
        self.assertIn("btc_coin", equity_curve.columns)
        self.assertIn("eth_coin", equity_curve.columns)


if __name__ == "__main__":
    unittest.main()
