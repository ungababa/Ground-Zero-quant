"""
Time-series cross-validation optimizer.

Loads N days of price history (default: 1 year), slices it into non-overlapping
2-week windows, and runs Optuna hyperparameter search where the objective is
the *average excess ROI* (strategy ROI minus buy-and-hold ROI) across every
window.  This surfaces parameter sets that genuinely outperform passive holding
across different market regimes rather than benefiting from a bull run.

Usage:
    uv run optimizer.py                        # 365 days, 14-day folds, 100 trials
    uv run optimizer.py --trials 200 --ticker BTC-USD
    uv run optimizer.py --csv data.csv --window-days 7 --trials 50
"""

import argparse
from datetime import timedelta

import optuna
import pandas as pd
from backtest import load_history, run_backtest
from config import GridConfig
from metrics import summarize_equity_curve

DEFAULT_YEAR_DAYS = 365
DEFAULT_WINDOW_DAYS = 14  # 2 weeks per fold


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


def _build_config(trial: optuna.Trial) -> GridConfig:
    levels = trial.suggest_int("levels_per_side", 5, 20)
    return GridConfig(
        spacing_pct=trial.suggest_float("spacing_pct", 0.0001, 0.05, log=True),
        levels_per_side=levels,
        per_level_notional_usd=trial.suggest_float(
            "per_level_notional_usd", 5_000.0, 100_000.0, step=500.0
        ),
        max_position_notional_usd=1_000_000.0,
        refresh_threshold_pct=trial.suggest_float(
            "refresh_threshold_pct", 0.0001, 0.05, log=True
        ),
        max_open_orders=levels * 2,
    )


def _config_from_params(params: dict) -> GridConfig:
    levels = params["levels_per_side"]
    return GridConfig(
        spacing_pct=params["spacing_pct"],
        levels_per_side=levels,
        per_level_notional_usd=params["per_level_notional_usd"],
        max_position_notional_usd=1_000_000.0,
        refresh_threshold_pct=params["refresh_threshold_pct"],
        max_open_orders=levels * 2,
    )


def excess_roi(equity_curve: pd.DataFrame) -> float:
    """Strategy ROI minus buy-and-hold ROI for the same window."""
    strat_roi = equity_curve["equity"].iloc[-1] / equity_curve["equity"].iloc[0] - 1.0
    bh_roi = equity_curve["bh_equity"].iloc[-1] / equity_curve["bh_equity"].iloc[0] - 1.0
    return float(strat_roi - bh_roi)


def evaluate_config_on_windows(
    config: GridConfig, windows: list[pd.DataFrame]
) -> list[float]:
    """Return per-window excess ROI (strategy − buy-and-hold) for *config*."""
    return [excess_roi(run_backtest(config, w)[0]) for w in windows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward (time-series CV) grid strategy optimizer.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_YEAR_DAYS,
        help="Total history to load in days.",
    )
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
        help="Length of each CV fold in days.",
    )
    parser.add_argument("--csv", type=str, default=None, help="Local CSV override.")
    parser.add_argument("--trials", type=int, default=50, help="Optuna trial count.")
    parser.add_argument("--ticker", type=str, default="SOL-USD", help="yfinance ticker.")
    parser.add_argument(
        "--top", type=int, default=5, help="Number of top strategies to display."
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()

    print(f"Loading {args.days} days of history for {args.ticker} …")
    history = load_history(args.days, args.csv, ticker=args.ticker)

    windows = split_into_windows(history, args.window_days)
    print(
        f"Split into {len(windows)} non-overlapping {args.window_days}-day windows.\n"
    )
    for i, w in enumerate(windows):
        print(
            f"  [{i + 1:>2}] {w['timestamp'].min().date()} → "
            f"{w['timestamp'].max().date()}  ({len(w)} bars)"
        )

    # -----------------------------------------------------------------------
    # Optuna study — objective = average excess ROI vs buy-and-hold
    # -----------------------------------------------------------------------

    def objective(trial: optuna.Trial) -> float:
        config = _build_config(trial)
        excess = evaluate_config_on_windows(config, windows)
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
        for k, v in trial.params.items():
            print(f"        {k}: {v}")

    # -----------------------------------------------------------------------
    # Detailed per-window breakdown for the best strategy
    # -----------------------------------------------------------------------

    best_config = _config_from_params(study.best_params)

    print("\n" + "=" * 60)
    print("BEST STRATEGY — per-window breakdown")
    print("=" * 60)
    excess_rois: list[float] = []
    for i, w in enumerate(windows):
        equity_curve, _ = run_backtest(best_config, w)
        m = summarize_equity_curve(equity_curve)
        ex = excess_roi(equity_curve)
        excess_rois.append(ex)
        bh_roi_w = equity_curve["bh_equity"].iloc[-1] / equity_curve["bh_equity"].iloc[0] - 1.0
        print(
            f"  [{i + 1:>2}] {w['timestamp'].min().date()} → "
            f"{w['timestamp'].max().date()} | "
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
    print(
        f"  Consistency    : "
        f"{sum(1 for r in excess_rois if r > 0)}/{len(excess_rois)} windows beat B&H"
    )
    print("\n  Best params :", study.best_params)
