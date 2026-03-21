"""
Time-series cross-validation optimizer.

Optimizes a weighted two-asset dual-grid portfolio where each asset has its own
grid parameters. Objective is average excess ROI (strategy minus weighted
buy-and-hold) across time windows.
"""

import argparse
from datetime import timedelta

import optuna
import pandas as pd

from backtest import load_history, run_weighted_dual_backtest
from config import GridConfig
from metrics import summarize_equity_curve

DEFAULT_YEAR_DAYS = 60
DEFAULT_WINDOW_DAYS = 10  # 2 weeks per fold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_into_windows(history: pd.DataFrame, window_days: int) -> list[pd.DataFrame]:
    """Slice *history* into consecutive non-overlapping windows of *window_days* each."""
    windows: list[pd.DataFrame] = []
    delta = timedelta(days=window_days)
    cursor = history["timestamp"].min()
    end = history["timestamp"].max()

    while cursor + delta <= end:
        mask = (history["timestamp"] >= cursor) & (history["timestamp"] < cursor + delta)
        chunk = history[mask].reset_index(drop=True)
        if not chunk.empty:
            windows.append(chunk)
        cursor += delta

    return windows


def split_histories_into_windows(histories: dict[str, pd.DataFrame], window_days: int) -> list[dict[str, pd.DataFrame]]:
    reference = next(iter(histories.values()))
    windows_ref = split_into_windows(reference, window_days)
    windows: list[dict[str, pd.DataFrame]] = []

    for w in windows_ref:
        start = w["timestamp"].min()
        end = w["timestamp"].max()
        chunk_by_symbol: dict[str, pd.DataFrame] = {}
        all_non_empty = True

        for symbol, history in histories.items():
            mask = (history["timestamp"] >= start) & (history["timestamp"] <= end)
            chunk = history[mask].reset_index(drop=True)
            if chunk.empty:
                all_non_empty = False
                break
            chunk_by_symbol[symbol] = chunk

        if all_non_empty:
            windows.append(chunk_by_symbol)

    return windows


def _ticker_to_pair(ticker: str) -> str:
    return ticker.replace("-", "/")


def _symbol_from_ticker(ticker: str, fallback: str) -> str:
    head = ticker.split("-")[0].strip().upper()
    return head if head else fallback


def _build_asset_config(trial: optuna.Trial, prefix: str, pair: str) -> GridConfig:
    levels = trial.suggest_int(f"{prefix}_levels_per_side", 3, 20)
    return GridConfig(
        pair=pair,
        spacing_pct=trial.suggest_float(f"{prefix}_spacing_pct", 0.0001, 0.05, log=True),
        levels_per_side=levels,
        per_level_notional_usd=trial.suggest_float(f"{prefix}_per_level_notional_usd", 1, 100_000.0, step=1.0),
        max_position_notional_usd=1_000_000.0,
        refresh_threshold_pct=trial.suggest_float(f"{prefix}_refresh_threshold_pct", 0.00001, 1, log=True),
        max_open_orders=levels * 2,
    )


def _asset_config_from_params(params: dict, prefix: str, pair: str) -> GridConfig:
    levels = int(params[f"{prefix}_levels_per_side"])
    return GridConfig(
        pair=pair,
        spacing_pct=float(params[f"{prefix}_spacing_pct"]),
        levels_per_side=levels,
        per_level_notional_usd=float(params[f"{prefix}_per_level_notional_usd"]),
        max_position_notional_usd=1_000_000.0,
        refresh_threshold_pct=float(params[f"{prefix}_refresh_threshold_pct"]),
        max_open_orders=levels * 2,
    )


def excess_roi(equity_curve: pd.DataFrame) -> float:
    """Strategy ROI minus buy-and-hold ROI for the same window."""
    strat_roi = equity_curve["equity"].iloc[-1] / equity_curve["equity"].iloc[0] - 1.0
    bh_roi = equity_curve["bh_equity"].iloc[-1] / equity_curve["bh_equity"].iloc[0] - 1.0
    return float(strat_roi - bh_roi)


def evaluate_config_on_windows(
    configs: dict[str, GridConfig],
    weights: dict[str, float],
    windows: list[dict[str, pd.DataFrame]],
    invested_ratio: float,
) -> list[float]:
    result: list[float] = []
    for window_histories in windows:
        equity_curve, _ = run_weighted_dual_backtest(
            configs=configs,
            histories=window_histories,
            weights=weights,
            invested_ratio=invested_ratio,
        )
        result.append(excess_roi(equity_curve))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward (time-series CV) grid strategy optimizer.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_YEAR_DAYS,
        help="Total history to load in days.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="Length of each CV fold in days.",
    )
    parser.add_argument("--csv", type=str, default=None, help="Local CSV override for both assets.")
    parser.add_argument("--trials", type=int, default=200, help="Optuna trial count.")
    parser.add_argument("--ticker-a", type=str, default="BTC-USD", help="Asset A yfinance ticker.")
    parser.add_argument("--ticker-b", type=str, default="SOL-USD", help="Asset B yfinance ticker.")
    parser.add_argument(
        "--invested-ratio",
        type=float,
        default=0.5,
        help="Fraction of total capital initially deployed across both assets.",
    )
    parser.add_argument(
        "--fixed-weight-a",
        type=float,
        default=None,
        help="Optional fixed Asset A weight in deployed capital (Asset B weight is 1-Asset A).",
    )
    parser.add_argument("--top", type=int, default=5, help="Number of top strategies to display.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()

    symbol_a = _symbol_from_ticker(args.ticker_a, "ASSET_A")
    symbol_b = _symbol_from_ticker(args.ticker_b, "ASSET_B")
    if symbol_a == symbol_b:
        symbol_b = f"{symbol_b}_2"

    print(f"Loading {args.days} days of history for {args.ticker_a} and {args.ticker_b} …")
    histories = {
        symbol_a: load_history(args.days, args.csv, ticker=args.ticker_a),
        symbol_b: load_history(args.days, args.csv, ticker=args.ticker_b),
    }

    windows = split_histories_into_windows(histories, args.window_days)
    print(f"Split into {len(windows)} non-overlapping {args.window_days}-day windows.\n")
    for i, w in enumerate(windows):
        base_window = w[symbol_a]
        print(
            f"  [{i + 1:>2}] {base_window['timestamp'].min().date()} → "
            f"{base_window['timestamp'].max().date()}  ({len(base_window)} bars)"
        )

    # -----------------------------------------------------------------------
    # Optuna study — objective = average excess ROI vs buy-and-hold
    # -----------------------------------------------------------------------

    def objective(trial: optuna.Trial) -> float:
        weight_a = (
            args.fixed_weight_a
            if args.fixed_weight_a is not None
            else trial.suggest_float("weight_a", 0.05, 0.95)
        )
        weight_b = 1.0 - float(weight_a)
        configs = {
            symbol_a: _build_asset_config(trial, "a", _ticker_to_pair(args.ticker_a)),
            symbol_b: _build_asset_config(trial, "b", _ticker_to_pair(args.ticker_b)),
        }
        excess = evaluate_config_on_windows(
            configs=configs,
            weights={symbol_a: float(weight_a), symbol_b: weight_b},
            windows=windows,
            invested_ratio=args.invested_ratio,
        )
        return sum(excess) / len(excess)

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize")

    print(f"\nRunning {args.trials} Optuna trials …\n")
    study.optimize(objective, n_trials=args.trials, show_progress_bar=True)

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------

    print("\n" + "=" * 60)
    print("RESULTS — ranked by average excess ROI vs buy-and-hold")
    print("=" * 60)

    completed = sorted(
        [t for t in study.trials if t.value is not None],
        key=lambda t: t.value,
        reverse=True,
    )

    top_n = min(args.top, len(completed))
    for rank, trial in enumerate(completed[:top_n], start=1):
        print(f"\n  #{rank}  avg excess ROI = {trial.value:+.4%}  (trial {trial.number})")
        weight_a = trial.params.get("weight_a", args.fixed_weight_a)
        if weight_a is not None:
            print(f"        {symbol_a.lower()}_weight: {float(weight_a):.4f}")
            print(f"        {symbol_b.lower()}_weight: {1.0 - float(weight_a):.4f}")
        for k, v in trial.params.items():
            print(f"        {k}: {v}")

    # -----------------------------------------------------------------------
    # Detailed per-window breakdown for the best strategy
    # -----------------------------------------------------------------------

    best_weight_a = study.best_params.get("weight_a", args.fixed_weight_a)
    if best_weight_a is None:
        raise ValueError("Unable to determine Asset A weight from optimizer results")
    best_weights = {
        symbol_a: float(best_weight_a),
        symbol_b: 1.0 - float(best_weight_a),
    }
    best_configs = {
        symbol_a: _asset_config_from_params(study.best_params, "a", _ticker_to_pair(args.ticker_a)),
        symbol_b: _asset_config_from_params(study.best_params, "b", _ticker_to_pair(args.ticker_b)),
    }

    print("\n" + "=" * 60)
    print(f"BEST WEIGHTED {symbol_a}/{symbol_b} STRATEGY — per-window breakdown")
    print("=" * 60)
    excess_rois: list[float] = []
    for i, w in enumerate(windows):
        equity_curve, _ = run_weighted_dual_backtest(
            configs=best_configs,
            histories=w,
            weights=best_weights,
            invested_ratio=args.invested_ratio,
        )
        base_window = w[symbol_a]
        m = summarize_equity_curve(equity_curve)
        ex = excess_roi(equity_curve)
        excess_rois.append(ex)
        bh_roi_w = equity_curve["bh_equity"].iloc[-1] / equity_curve["bh_equity"].iloc[0] - 1.0
        print(
            f"  [{i + 1:>2}] {base_window['timestamp'].min().date()} → "
            f"{base_window['timestamp'].max().date()} | "
            f"strat={m['total_return']:+.3%}  "
            f"B&H={bh_roi_w:+.3%}  "
            f"excess={ex:+.3%}  "
            f"Sharpe={m['sharpe']:+.2f}  "
            f"MDD={m['max_drawdown']:.3%}"
        )

    avg_excess = sum(excess_rois) / len(excess_rois)
    print(f"\n  Avg excess ROI : {avg_excess:+.4%}")
    print(f"  Min excess ROI : {min(excess_rois):+.4%}")
    print(f"  Max excess ROI : {max(excess_rois):+.4%}")
    print(f"  Consistency    : {sum(1 for r in excess_rois if r > 0)}/{len(excess_rois)} windows beat B&H")
    print(
        f"  Best weights   : "
        f"{symbol_a}={best_weights[symbol_a]:.4f}, {symbol_b}={best_weights[symbol_b]:.4f}"
    )
    print("\n  Best params :", study.best_params)
