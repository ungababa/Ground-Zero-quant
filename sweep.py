from __future__ import annotations

import csv
import itertools
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from metrics import summarize_equity_curve

BACKTEST_FILE = "backtest.py"
ARTIFACTS_DIR = Path("artifacts")
OUTPUT_PREFIX = "sweep_tp_offsets_next10"
INITIAL_CAPITAL = 1_000_000.0
TOP_N = 20

PAIRS = ["ETH/USD", "SOL/USD", "BTC/USD"]
DAYS_LIST = [21, 60]

BASE_PARAMS = {
    "GRID_SPACING_PCT": 0.011,
    "GRID_BUY_SPACING_MULTIPLIER": 1.0,
    "GRID_SELL_SPACING_MULTIPLIER": 1.0,
    "GRID_LEVELS_PER_SIDE": 25,
    "GRID_PER_LEVEL_NOTIONAL_USD": 17000,
}

WEIGHT_CANDIDATES = [
    {"ETH/USD": 0.45, "SOL/USD": 0.35, "BTC/USD": 0.20},
    {"ETH/USD": 0.50, "SOL/USD": 0.25, "BTC/USD": 0.25},
    {"ETH/USD": 0.50, "SOL/USD": 0.40, "BTC/USD": 0.10},
]

TP_GRID = {
    "GRID_NEAREST_BUY_OFFSET_MULTIPLIER": [0.75, 0.90, 1.00, 1.10, 1.25],
    "GRID_NEAREST_SELL_OFFSET_MULTIPLIER": [0.75, 0.90, 1.00, 1.10, 1.25],
}


def safe_pair_name(pair: str) -> str:
    return pair.replace("/", "_")


def compound_return(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float((1.0 + series).prod() - 1.0)


def infer_periods_per_year(timestamps: pd.Series) -> float:
    ts = pd.to_datetime(timestamps, utc=True).sort_values()
    if len(ts) < 2:
        return 365.0 * 24.0
    diffs = ts.diff().dropna().dt.total_seconds()
    median_seconds = float(diffs.median()) if len(diffs) else 3600.0
    if median_seconds <= 0:
        median_seconds = 3600.0
    return (365.0 * 24.0 * 3600.0) / median_seconds


def compute_drawdown_duration(equity: pd.Series, timestamps: pd.Series) -> dict:
    eq = equity.astype(float).reset_index(drop=True)
    ts = pd.to_datetime(timestamps, utc=True).reset_index(drop=True)

    running_max = eq.cummax()
    underwater = eq < running_max

    max_duration_hours = 0.0
    current_start = None
    total_underwater_bars = 0

    for i, is_underwater in enumerate(underwater):
        if is_underwater:
            total_underwater_bars += 1
            if current_start is None:
                current_start = i
        else:
            if current_start is not None:
                duration_hours = (ts.iloc[i - 1] - ts.iloc[current_start]).total_seconds() / 3600.0
                max_duration_hours = max(max_duration_hours, duration_hours)
                current_start = None

    if current_start is not None:
        duration_hours = (ts.iloc[len(ts) - 1] - ts.iloc[current_start]).total_seconds() / 3600.0
        max_duration_hours = max(max_duration_hours, duration_hours)

    underwater_ratio = total_underwater_bars / len(eq) if len(eq) else 0.0

    return {
        "max_drawdown_duration_hours": float(max_duration_hours),
        "time_underwater_ratio": float(underwater_ratio),
    }


def compute_recent_window_metrics(equity_df: pd.DataFrame, days: int) -> dict:
    df = equity_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    cutoff = df["timestamp"].max() - pd.Timedelta(days=days)

    window = df[df["timestamp"] >= cutoff].copy()
    if len(window) < 2:
        return {
            f"last_{days}d_return": 0.0,
            f"last_{days}d_bh_return": 0.0,
            f"last_{days}d_alpha_vs_bh": 0.0,
            f"last_{days}d_sharpe": 0.0,
            f"last_{days}d_sortino": 0.0,
        }

    strat_ret = window["equity"].astype(float).pct_change().dropna()
    bh_ret = window["bh_equity"].astype(float).pct_change().dropna()

    periods_per_year = infer_periods_per_year(window["timestamp"])

    strat_total = float(window["equity"].iloc[-1] / window["equity"].iloc[0] - 1.0)
    bh_total = float(window["bh_equity"].iloc[-1] / window["bh_equity"].iloc[0] - 1.0)

    mean_ret = float(strat_ret.mean()) if len(strat_ret) else 0.0
    vol = float(strat_ret.std(ddof=0)) if len(strat_ret) else 0.0
    downside = strat_ret[strat_ret < 0]
    downside_vol = float(downside.std(ddof=0)) if len(downside) else 0.0

    sharpe = 0.0 if vol == 0 else (mean_ret / vol) * math.sqrt(periods_per_year)
    sortino = 0.0 if downside_vol == 0 else (mean_ret / downside_vol) * math.sqrt(periods_per_year)

    return {
        f"last_{days}d_return": strat_total,
        f"last_{days}d_bh_return": bh_total,
        f"last_{days}d_alpha_vs_bh": strat_total - bh_total,
        f"last_{days}d_sharpe": sharpe,
        f"last_{days}d_sortino": sortino,
    }


def compute_regime_metrics(equity_df: pd.DataFrame) -> dict:
    df = equity_df.copy()
    strat_ret = df["equity"].astype(float).pct_change().fillna(0.0)
    bench_ret = df["bh_equity"].astype(float).pct_change().fillna(0.0)

    up_mask = bench_ret > 0
    down_mask = bench_ret < 0

    strategy_total_return = float(df["equity"].iloc[-1] / df["equity"].iloc[0] - 1.0)
    benchmark_total_return = float(df["bh_equity"].iloc[-1] / df["bh_equity"].iloc[0] - 1.0)

    strategy_up_return = compound_return(strat_ret[up_mask])
    benchmark_up_return = compound_return(bench_ret[up_mask])

    strategy_down_return = compound_return(strat_ret[down_mask])
    benchmark_down_return = compound_return(bench_ret[down_mask])

    upside_capture_ratio = 0.0
    if benchmark_up_return > 0:
        upside_capture_ratio = strategy_up_return / benchmark_up_return

    downside_capture_ratio = 0.0
    if benchmark_down_return < 0:
        downside_capture_ratio = max(-strategy_down_return, 0.0) / abs(benchmark_down_return)

    total_alpha = strategy_total_return - benchmark_total_return
    upside_alpha = strategy_up_return - benchmark_up_return
    downside_alpha = strategy_down_return - benchmark_down_return

    positive_bar_hit_rate = float((strat_ret[up_mask] > 0).mean()) if up_mask.any() else 0.0
    negative_bar_profit_rate = float((strat_ret[down_mask] >= 0).mean()) if down_mask.any() else 0.0
    all_bar_win_rate = float((strat_ret > 0).mean()) if len(strat_ret) else 0.0

    return {
        "strategy_total_return": strategy_total_return,
        "benchmark_total_return": benchmark_total_return,
        "total_alpha_vs_bh": total_alpha,
        "strategy_up_return": strategy_up_return,
        "benchmark_up_return": benchmark_up_return,
        "upside_alpha_vs_bh": upside_alpha,
        "upside_capture_ratio": upside_capture_ratio,
        "strategy_down_return": strategy_down_return,
        "benchmark_down_return": benchmark_down_return,
        "downside_alpha_vs_bh": downside_alpha,
        "downside_capture_ratio": downside_capture_ratio,
        "positive_bar_hit_rate": positive_bar_hit_rate,
        "negative_bar_profit_rate": negative_bar_profit_rate,
        "all_bar_win_rate": all_bar_win_rate,
        "up_bars": int(up_mask.sum()),
        "down_bars": int(down_mask.sum()),
    }


def compute_trade_detail_metrics(
    trades_df: pd.DataFrame,
    detail_log_df: pd.DataFrame,
    equity_df: pd.DataFrame,
) -> dict:
    out = {
        "total_fees_paid": 0.0,
        "turnover_notional": 0.0,
        "avg_notional_per_trade": 0.0,
        "trades_per_day": 0.0,
        "fills_per_day": 0.0,
        "buy_trade_count": 0,
        "sell_trade_count": 0,
        "pause_ratio": 0.0,
        "avg_fill_count_per_bar": 0.0,
        "avg_coin_exposure_ratio": 0.0,
        "avg_cash_exposure_ratio": 0.0,
        "avg_equity_usd": 0.0,
        "avg_realized_pnl_usd": 0.0,
        "final_realized_pnl_usd": 0.0,
        "avg_unrealized_pnl_usd": 0.0,
        "fee_to_turnover_ratio": 0.0,
        "buy_locked_ratio": 0.0,
        "sell_locked_ratio": 0.0,
    }

    if not trades_df.empty:
        t = trades_df.copy()
        t["notional"] = t["price"].astype(float) * t["quantity"].astype(float)
        t["fee"] = t["fee"].astype(float)

        out["total_fees_paid"] = float(t["fee"].sum())
        out["turnover_notional"] = float(t["notional"].sum())
        out["avg_notional_per_trade"] = float(t["notional"].mean())
        out["buy_trade_count"] = int((t["side"] == "BUY").sum())
        out["sell_trade_count"] = int((t["side"] == "SELL").sum())

        if out["turnover_notional"] > 0:
            out["fee_to_turnover_ratio"] = out["total_fees_paid"] / out["turnover_notional"]

        if "timestamp" in t.columns and len(t) >= 2:
            t["timestamp"] = pd.to_datetime(t["timestamp"], utc=True)
            total_days = max((t["timestamp"].max() - t["timestamp"].min()).total_seconds() / 86400.0, 1e-9)
            out["trades_per_day"] = float(len(t) / total_days)

    if not detail_log_df.empty:
        d = detail_log_df.copy()
        if "paused" in d.columns:
            out["pause_ratio"] = float(d["paused"].astype(bool).mean())
        if "fill_count" in d.columns:
            out["avg_fill_count_per_bar"] = float(d["fill_count"].astype(float).mean())
        if "buy_locked" in d.columns:
            out["buy_locked_ratio"] = float(d["buy_locked"].astype(bool).mean())
        if "sell_locked" in d.columns:
            out["sell_locked_ratio"] = float(d["sell_locked"].astype(bool).mean())

        if {"coin_market_value_usd", "cash_usd", "equity_usd"}.issubset(d.columns):
            eq = d["equity_usd"].astype(float).replace(0, np.nan)
            out["avg_coin_exposure_ratio"] = float((d["coin_market_value_usd"].astype(float) / eq).fillna(0.0).mean())
            out["avg_cash_exposure_ratio"] = float((d["cash_usd"].astype(float) / eq).fillna(0.0).mean())
            out["avg_equity_usd"] = float(d["equity_usd"].astype(float).mean())

        if "realized_pnl_usd" in d.columns:
            out["avg_realized_pnl_usd"] = float(d["realized_pnl_usd"].astype(float).mean())
            out["final_realized_pnl_usd"] = float(d["realized_pnl_usd"].astype(float).iloc[-1])

        if "unrealized_pnl_usd" in d.columns:
            out["avg_unrealized_pnl_usd"] = float(d["unrealized_pnl_usd"].astype(float).mean())

        if "timestamp" in d.columns and len(d) >= 2:
            d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
            total_days = max((d["timestamp"].max() - d["timestamp"].min()).total_seconds() / 86400.0, 1e-9)
            out["fills_per_day"] = float(d["fill_count"].astype(float).sum() / total_days) if "fill_count" in d.columns else 0.0

    dd = compute_drawdown_duration(equity_df["equity"], equity_df["timestamp"])
    out.update(dd)

    out.update(compute_recent_window_metrics(equity_df, 7))
    out.update(compute_recent_window_metrics(equity_df, 14))

    eq_ret = equity_df["equity"].astype(float).pct_change().dropna()
    periods_per_year = infer_periods_per_year(equity_df["timestamp"])
    vol = float(eq_ret.std(ddof=0)) if len(eq_ret) else 0.0
    downside = eq_ret[eq_ret < 0]
    downside_vol = float(downside.std(ddof=0)) if len(downside) else 0.0

    out["annualized_volatility"] = vol * math.sqrt(periods_per_year) if vol > 0 else 0.0
    out["annualized_downside_volatility"] = downside_vol * math.sqrt(periods_per_year) if downside_vol > 0 else 0.0

    return out


def prepare_curve_for_portfolio(equity_curve: pd.DataFrame) -> pd.DataFrame:
    df = equity_curve.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    eq0 = float(df["equity"].iloc[0])
    bh0 = float(df["bh_equity"].iloc[0])

    df["norm_equity"] = df["equity"].astype(float) / eq0
    df["norm_bh_equity"] = df["bh_equity"].astype(float) / bh0

    return df[["timestamp", "norm_equity", "norm_bh_equity"]]


def run_backtest(pair: str, days: int, params: dict) -> dict:
    cmd = [
        sys.executable,
        BACKTEST_FILE,
        "--days",
        str(days),
        "--pair",
        pair,
        "--spacing-pct",
        str(params["GRID_SPACING_PCT"]),
        "--buy-spacing-multiplier",
        str(params["GRID_BUY_SPACING_MULTIPLIER"]),
        "--sell-spacing-multiplier",
        str(params["GRID_SELL_SPACING_MULTIPLIER"]),
        "--levels-per-side",
        str(params["GRID_LEVELS_PER_SIDE"]),
        "--per-level-notional-usd",
        str(params["GRID_PER_LEVEL_NOTIONAL_USD"]),
        "--nearest-buy-offset-multiplier",
        str(params["GRID_NEAREST_BUY_OFFSET_MULTIPLIER"]),
        "--nearest-sell-offset-multiplier",
        str(params["GRID_NEAREST_SELL_OFFSET_MULTIPLIER"]),
        "--cash-reserve-pct",
        "0.05",
        "--disable-downward-refresh-when-no-cash",
        "--disable-upward-refresh-when-no-coin",
    ]

    completed = subprocess.run(cmd, capture_output=True, text=True)

    if completed.returncode != 0:
        raise RuntimeError(
            f"Backtest failed for {pair}, days={days}, params={params}.\n"
            f"COMMAND: {' '.join(cmd)}\n\n"
            f"STDOUT:\n{completed.stdout}\n\n"
            f"STDERR:\n{completed.stderr}"
        )

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"No output from backtest for {pair}, days={days}, params={params}.")

    try:
        summary = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse JSON output from backtest for {pair}, days={days}.\n"
            f"Last line:\n{lines[-1]}\n\n"
            f"Full STDOUT:\n{completed.stdout}"
        ) from exc

    equity_curve = pd.read_csv(ARTIFACTS_DIR / "equity_curve.csv")
    trades_df = pd.read_csv(ARTIFACTS_DIR / "trades.csv")
    detail_log_df = pd.read_csv(ARTIFACTS_DIR / "detailed_backtest_log.csv")

    regime_metrics = compute_regime_metrics(equity_curve)
    trade_detail_metrics = compute_trade_detail_metrics(trades_df, detail_log_df, equity_curve)

    return {
        "pair": pair,
        "days": days,
        "metrics": summary["metrics"],
        "trades": summary["trades"],
        "regime_metrics": regime_metrics,
        "trade_detail_metrics": trade_detail_metrics,
        "curve": prepare_curve_for_portfolio(equity_curve),
    }


def combine_curves_into_portfolio(curve_map: dict[str, pd.DataFrame], weights: dict[str, float]) -> pd.DataFrame:
    merged = None

    for pair in PAIRS:
        temp = curve_map[pair].copy()
        safe = safe_pair_name(pair)
        temp = temp.rename(
            columns={
                "norm_equity": f"{safe}_norm_equity",
                "norm_bh_equity": f"{safe}_norm_bh_equity",
            }
        )

        if merged is None:
            merged = temp
        else:
            merged = merged.merge(temp, on="timestamp", how="inner")

    merged = merged.sort_values("timestamp").reset_index(drop=True)

    merged["portfolio_norm_equity"] = 0.0
    merged["portfolio_norm_bh_equity"] = 0.0

    for pair in PAIRS:
        safe = safe_pair_name(pair)
        w = weights[pair]
        merged["portfolio_norm_equity"] += w * merged[f"{safe}_norm_equity"]
        merged["portfolio_norm_bh_equity"] += w * merged[f"{safe}_norm_bh_equity"]

    return pd.DataFrame(
        {
            "timestamp": merged["timestamp"],
            "equity": INITIAL_CAPITAL * merged["portfolio_norm_equity"],
            "bh_equity": INITIAL_CAPITAL * merged["portfolio_norm_bh_equity"],
        }
    )


def compute_cross_asset_summary(pair_results: dict[str, dict]) -> dict:
    returns = [float(pair_results[p]["metrics"].get("total_return", 0.0) or 0.0) for p in PAIRS]
    sharpes = [float(pair_results[p]["metrics"].get("sharpe", 0.0) or 0.0) for p in PAIRS]
    drawdowns = [abs(float(pair_results[p]["metrics"].get("max_drawdown", 0.0) or 0.0)) for p in PAIRS]

    return {
        "best_pair_return": max(returns),
        "worst_pair_return": min(returns),
        "mean_pair_return": float(np.mean(returns)),
        "std_pair_return": float(np.std(returns)),
        "mean_pair_sharpe": float(np.mean(sharpes)),
        "worst_pair_sharpe": min(sharpes),
        "mean_pair_drawdown": float(np.mean(drawdowns)),
        "worst_pair_drawdown": max(drawdowns),
        "consistency_score": float(np.mean(returns) - np.std(returns)),
    }


def rank_key(row: dict) -> tuple:
    m21 = row["portfolio_results"][21]["metrics"]
    r21 = row["portfolio_results"][21]["regime_metrics"]
    d21 = row["portfolio_results"][21]["detail_metrics"]
    x21 = row["portfolio_results"][21]["cross_asset_metrics"]

    m60 = row["portfolio_results"][60]["metrics"]
    r60 = row["portfolio_results"][60]["regime_metrics"]
    x60 = row["portfolio_results"][60]["cross_asset_metrics"]

    return (
        float(m21.get("total_return", 0.0) or 0.0),
        float(m21.get("sharpe", 0.0) or 0.0),
        float(m21.get("sortino", 0.0) or 0.0),
        float(m21.get("calmar", 0.0) or 0.0),
        float(r21.get("total_alpha_vs_bh", 0.0) or 0.0),
        float(d21.get("last_7d_return", 0.0) or 0.0),
        float(d21.get("last_14d_return", 0.0) or 0.0),
        -abs(float(m21.get("max_drawdown", 0.0) or 0.0)),
        -float(r21.get("downside_capture_ratio", 0.0) or 0.0),
        float(r21.get("upside_capture_ratio", 0.0) or 0.0),
        float(x21.get("consistency_score", 0.0) or 0.0),
        float(x21.get("worst_pair_return", 0.0) or 0.0),
        float(m60.get("total_return", 0.0) or 0.0),
        float(m60.get("sharpe", 0.0) or 0.0),
        float(m60.get("sortino", 0.0) or 0.0),
        float(m60.get("calmar", 0.0) or 0.0),
        -abs(float(m60.get("max_drawdown", 0.0) or 0.0)),
        -float(r60.get("downside_capture_ratio", 0.0) or 0.0),
        float(x60.get("consistency_score", 0.0) or 0.0),
    )


def score_for_csv(row: dict) -> float:
    m21 = row["portfolio_results"][21]["metrics"]
    r21 = row["portfolio_results"][21]["regime_metrics"]
    d21 = row["portfolio_results"][21]["detail_metrics"]
    x21 = row["portfolio_results"][21]["cross_asset_metrics"]

    m60 = row["portfolio_results"][60]["metrics"]
    r60 = row["portfolio_results"][60]["regime_metrics"]

    return (
        1200.0 * float(m21.get("total_return", 0.0) or 0.0)
        + 45.0 * float(m21.get("sharpe", 0.0) or 0.0)
        + 35.0 * float(m21.get("sortino", 0.0) or 0.0)
        + 20.0 * float(m21.get("calmar", 0.0) or 0.0)
        + 12.0 * float(r21.get("total_alpha_vs_bh", 0.0) or 0.0)
        + 10.0 * float(d21.get("last_7d_return", 0.0) or 0.0)
        + 6.0 * float(d21.get("last_14d_return", 0.0) or 0.0)
        - 10.0 * abs(float(m21.get("max_drawdown", 0.0) or 0.0))
        - 6.0 * float(r21.get("downside_capture_ratio", 0.0) or 0.0)
        + 3.0 * float(r21.get("upside_capture_ratio", 0.0) or 0.0)
        + 4.0 * float(x21.get("consistency_score", 0.0) or 0.0)
        + 400.0 * float(m60.get("total_return", 0.0) or 0.0)
        + 10.0 * float(m60.get("sharpe", 0.0) or 0.0)
        + 8.0 * float(m60.get("sortino", 0.0) or 0.0)
        + 6.0 * float(m60.get("calmar", 0.0) or 0.0)
        - 4.0 * abs(float(m60.get("max_drawdown", 0.0) or 0.0))
        - 2.0 * float(r60.get("downside_capture_ratio", 0.0) or 0.0)
    )


def flatten_result(row: dict) -> dict:
    flat = {}
    flat.update(BASE_PARAMS)
    flat.update(row["tp_params"])

    flat["weight_ETH"] = row["weights"]["ETH/USD"]
    flat["weight_SOL"] = row["weights"]["SOL/USD"]
    flat["weight_BTC"] = row["weights"]["BTC/USD"]

    for days in DAYS_LIST:
        m = row["portfolio_results"][days]["metrics"]
        r = row["portfolio_results"][days]["regime_metrics"]
        d = row["portfolio_results"][days]["detail_metrics"]
        x = row["portfolio_results"][days]["cross_asset_metrics"]

        flat.update({f"d{days}_portfolio_metric_{k}": v for k, v in m.items()})
        flat.update({f"d{days}_portfolio_regime_{k}": v for k, v in r.items()})
        flat.update({f"d{days}_portfolio_detail_{k}": v for k, v in d.items()})
        flat.update({f"d{days}_portfolio_cross_{k}": v for k, v in x.items()})

        for pair in PAIRS:
            safe = safe_pair_name(pair)
            pm = row["pair_results"][days][pair]["metrics"]
            pr = row["pair_results"][days][pair]["regime_metrics"]
            pdm = row["pair_results"][days][pair]["trade_detail_metrics"]

            flat[f"d{days}_{safe}_trades"] = row["pair_results"][days][pair]["trades"]
            flat.update({f"d{days}_{safe}_metric_{k}": v for k, v in pm.items()})
            flat.update({f"d{days}_{safe}_regime_{k}": v for k, v in pr.items()})
            flat.update({f"d{days}_{safe}_detail_{k}": v for k, v in pdm.items()})

    flat["score"] = row["score"]
    return flat


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    tp_keys = list(TP_GRID.keys())
    tp_combos = [dict(zip(tp_keys, combo)) for combo in itertools.product(*[TP_GRID[k] for k in tp_keys])]

    results = []
    cache: dict[tuple[int, str, tuple], dict] = {}

    total_backtests = len(DAYS_LIST) * len(PAIRS) * len(tp_combos)
    run_index = 0

    for days in DAYS_LIST:
        for tp_params in tp_combos:
            combined_params = {**BASE_PARAMS, **tp_params}
            tp_key = tuple(tp_params[k] for k in tp_keys)

            for pair in PAIRS:
                run_index += 1
                print(f"[{run_index}/{total_backtests}] days={days} | {pair} | {tp_params}")
                cache[(days, pair, tp_key)] = run_backtest(pair, days, combined_params)

    for tp_params in tp_combos:
        tp_key = tuple(tp_params[k] for k in tp_keys)

        pair_results_by_days = {}
        for days in DAYS_LIST:
            pair_results_by_days[days] = {
                pair: cache[(days, pair, tp_key)]
                for pair in PAIRS
            }

        for weights in WEIGHT_CANDIDATES:
            row = {
                "tp_params": tp_params,
                "weights": weights,
                "pair_results": {},
                "portfolio_results": {},
            }

            for days in DAYS_LIST:
                row["pair_results"][days] = {
                    pair: {
                        "metrics": pair_results_by_days[days][pair]["metrics"],
                        "regime_metrics": pair_results_by_days[days][pair]["regime_metrics"],
                        "trade_detail_metrics": pair_results_by_days[days][pair]["trade_detail_metrics"],
                        "trades": pair_results_by_days[days][pair]["trades"],
                    }
                    for pair in PAIRS
                }

                portfolio_curve = combine_curves_into_portfolio(
                    {pair: pair_results_by_days[days][pair]["curve"] for pair in PAIRS},
                    weights,
                )

                portfolio_metrics = summarize_equity_curve(portfolio_curve)
                portfolio_regime_metrics = compute_regime_metrics(portfolio_curve)
                portfolio_detail_metrics = {}
                portfolio_detail_metrics.update(compute_drawdown_duration(portfolio_curve["equity"], portfolio_curve["timestamp"]))
                portfolio_detail_metrics.update(compute_recent_window_metrics(portfolio_curve, 7))
                portfolio_detail_metrics.update(compute_recent_window_metrics(portfolio_curve, 14))

                eq_ret = portfolio_curve["equity"].astype(float).pct_change().dropna()
                periods_per_year = infer_periods_per_year(portfolio_curve["timestamp"])
                vol = float(eq_ret.std(ddof=0)) if len(eq_ret) else 0.0
                downside = eq_ret[eq_ret < 0]
                downside_vol = float(downside.std(ddof=0)) if len(downside) else 0.0

                portfolio_detail_metrics["annualized_volatility"] = vol * math.sqrt(periods_per_year) if vol > 0 else 0.0
                portfolio_detail_metrics["annualized_downside_volatility"] = downside_vol * math.sqrt(periods_per_year) if downside_vol > 0 else 0.0

                cross_asset_metrics = compute_cross_asset_summary(row["pair_results"][days])

                row["portfolio_results"][days] = {
                    "metrics": portfolio_metrics,
                    "regime_metrics": portfolio_regime_metrics,
                    "detail_metrics": portfolio_detail_metrics,
                    "cross_asset_metrics": cross_asset_metrics,
                }

            row["score"] = score_for_csv(row)
            results.append(row)

    ranked = sorted(results, key=rank_key, reverse=True)

    Path(f"{OUTPUT_PREFIX}_results.json").write_text(json.dumps(ranked, indent=2))
    write_csv(Path(f"{OUTPUT_PREFIX}_results.csv"), [flatten_result(r) for r in ranked])

    print("\nTOP TP SETTINGS FOR NEXT 10 DAYS\n")
    for i, row in enumerate(ranked[:TOP_N], start=1):
        tp = row["tp_params"]
        w = row["weights"]
        m21 = row["portfolio_results"][21]["metrics"]
        r21 = row["portfolio_results"][21]["regime_metrics"]
        d21 = row["portfolio_results"][21]["detail_metrics"]
        x21 = row["portfolio_results"][21]["cross_asset_metrics"]

        print(
            f"{i:>2}. "
            f"buy_offset_mult={tp['GRID_NEAREST_BUY_OFFSET_MULTIPLIER']:.2f} | "
            f"sell_offset_mult={tp['GRID_NEAREST_SELL_OFFSET_MULTIPLIER']:.2f} | "
            f"weights={w} | "
            f"21d_return={m21.get('total_return', 0.0):.4%} | "
            f"21d_dd={m21.get('max_drawdown', 0.0):.4%} | "
            f"21d_sharpe={m21.get('sharpe', 0.0):.3f} | "
            f"21d_sortino={m21.get('sortino', 0.0):.3f} | "
            f"21d_calmar={m21.get('calmar', 0.0):.3f} | "
            f"21d_last7={d21.get('last_7d_return', 0.0):.4%} | "
            f"21d_last14={d21.get('last_14d_return', 0.0):.4%} | "
            f"21d_up_capture={r21.get('upside_capture_ratio', 0.0):.3f} | "
            f"21d_down_capture={r21.get('downside_capture_ratio', 0.0):.3f} | "
            f"21d_worst_pair={x21.get('worst_pair_return', 0.0):.4%} | "
            f"21d_consistency={x21.get('consistency_score', 0.0):.4%}"
        )

    print(
        "\nSaved files:\n"
        f"  - {OUTPUT_PREFIX}_results.csv\n"
        f"  - {OUTPUT_PREFIX}_results.json"
    )


if __name__ == "__main__":
    main()