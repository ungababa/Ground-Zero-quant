from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import pandas as pd

from backtest.backtest_portfolio_engine import (
    PAIRS,
    TARGET_WEIGHTS,
    load_histories,
    simulate_portfolio,
    window_summaries,
    write_outputs,
)


def parse_float_list(value: str) -> list[float]:
    return [float(x.strip()) for x in value.split(",") if x.strip()]


def parse_int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def build_benchmark_returns(histories: dict[str, pd.DataFrame]) -> pd.Series:
    base = histories["ETH/USD"][["timestamp"]].copy()
    bench = pd.Series(0.0, index=base.index, dtype=float)

    for pair in PAIRS:
        close = histories[pair]["close"].astype(float).reset_index(drop=True)
        bench += TARGET_WEIGHTS[pair] * (close / close.iloc[0])

    returns = bench.pct_change().fillna(0.0)
    returns.index = base["timestamp"]
    return returns


def compute_upside_metrics(merged: pd.DataFrame, benchmark_returns: pd.Series) -> dict[str, float]:
    bot_returns = merged["equity"].pct_change().fillna(0.0)
    bot_returns.index = merged["timestamp"]

    aligned_bench = benchmark_returns.reindex(bot_returns.index).fillna(0.0)
    up_mask = aligned_bench > 0
    down_mask = aligned_bench < 0

    avg_bot_up = float(bot_returns[up_mask].mean()) if up_mask.any() else 0.0
    avg_bench_up = float(aligned_bench[up_mask].mean()) if up_mask.any() else 0.0
    avg_bot_down = float(bot_returns[down_mask].mean()) if down_mask.any() else 0.0
    avg_bench_down = float(aligned_bench[down_mask].mean()) if down_mask.any() else 0.0

    upside_capture = (avg_bot_up / avg_bench_up) if avg_bench_up > 0 else 0.0
    downside_capture = (avg_bot_down / avg_bench_down) if avg_bench_down < 0 else 0.0

    return {
        "avg_bot_return_when_benchmark_up": avg_bot_up,
        "avg_benchmark_return_when_up": avg_bench_up,
        "upside_capture_ratio": upside_capture,
        "avg_bot_return_when_benchmark_down": avg_bot_down,
        "avg_benchmark_return_when_down": avg_bench_down,
        "downside_capture_ratio": downside_capture,
        "up_bar_count": int(up_mask.sum()),
        "down_bar_count": int(down_mask.sum()),
    }


def score_one_horizon(summary: dict, upside: dict, full_label: str) -> float:
    full_window = summary[full_label]
    trailing_21 = summary["trailing_21d"]
    trailing_10 = summary["trailing_10d"]

    upside_capture = upside["upside_capture_ratio"]
    downside_capture = upside["downside_capture_ratio"]

    # Competition-oriented score:
    # prioritize recent returns and upside, but still penalize drawdown/cash/drift
    score = 0.0

    score += 1.65 * trailing_10["total_return"]
    score += 1.20 * trailing_21["total_return"]
    score += 0.35 * full_window["total_return"]

    score += 0.30 * upside_capture
    score += 0.20 * (upside_capture - downside_capture)

    score -= 0.35 * abs(trailing_10["max_drawdown"])
    score -= 0.20 * abs(trailing_21["max_drawdown"])
    score -= 0.08 * abs(full_window["max_drawdown"])

    score -= 0.08 * trailing_10["avg_cash_weight"]
    score -= 0.05 * trailing_21["avg_cash_weight"]
    score -= 0.05 * trailing_21["avg_abs_weight_drift"]

    return float(score)


def mode_to_overrides(
    mode: str,
    *,
    trigger_pct: float,
    slope: float,
    max_mult: float,
    min_mult: float,
) -> dict:
    if mode == "baseline_no_move":
        return {
            "buy_move_sizing_enabled": False,
            "buy_move_size_trigger_pct": 0.01,
            "buy_move_size_slope": 0.0,
            "buy_move_size_max_mult": 1.0,
            "buy_move_size_min_mult": 1.0,
            "sell_move_sizing_enabled": False,
            "sell_move_size_trigger_pct": 0.01,
            "sell_move_size_slope": 0.0,
            "sell_move_size_max_mult": 1.0,
            "sell_move_size_min_mult": 1.0,
        }

    if mode == "buy_only":
        return {
            "buy_move_sizing_enabled": True,
            "buy_move_size_trigger_pct": trigger_pct,
            "buy_move_size_slope": slope,
            "buy_move_size_max_mult": max_mult,
            "buy_move_size_min_mult": min_mult,
            "sell_move_sizing_enabled": False,
            "sell_move_size_trigger_pct": 0.01,
            "sell_move_size_slope": 0.0,
            "sell_move_size_max_mult": 1.0,
            "sell_move_size_min_mult": 1.0,
        }

    if mode == "sell_only":
        return {
            "buy_move_sizing_enabled": False,
            "buy_move_size_trigger_pct": 0.01,
            "buy_move_size_slope": 0.0,
            "buy_move_size_max_mult": 1.0,
            "buy_move_size_min_mult": 1.0,
            "sell_move_sizing_enabled": True,
            "sell_move_size_trigger_pct": trigger_pct,
            "sell_move_size_slope": slope,
            "sell_move_size_max_mult": max_mult,
            "sell_move_size_min_mult": min_mult,
        }

    if mode == "both":
        return {
            "buy_move_sizing_enabled": True,
            "buy_move_size_trigger_pct": trigger_pct,
            "buy_move_size_slope": slope,
            "buy_move_size_max_mult": max_mult,
            "buy_move_size_min_mult": min_mult,
            "sell_move_sizing_enabled": True,
            "sell_move_size_trigger_pct": trigger_pct,
            "sell_move_size_slope": slope,
            "sell_move_size_max_mult": max_mult,
            "sell_move_size_min_mult": min_mult,
        }

    raise ValueError(f"Unknown mode: {mode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-grid", type=str, default="45,60,75")
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--eth-csv", type=str, default=None)
    parser.add_argument("--sol-csv", type=str, default=None)
    parser.add_argument("--btc-csv", type=str, default=None)
    parser.add_argument("--outdir", type=str, default="artifacts_dynamic_mode_compare")

    # We want smaller-than-base initially, then larger on bigger moves
    parser.add_argument("--trigger-pcts", type=str, default="0.005,0.0075,0.01")
    parser.add_argument("--slopes", type=str, default="0.20,0.35,0.50")
    parser.add_argument("--max-mults", type=str, default="1.2,1.4,1.6")
    parser.add_argument("--min-mults", type=str, default="0.75,0.85,0.95")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    days_grid = parse_int_list(args.days_grid)
    trigger_pcts = parse_float_list(args.trigger_pcts)
    slopes = parse_float_list(args.slopes)
    max_mults = parse_float_list(args.max_mults)
    min_mults = parse_float_list(args.min_mults)

    histories_by_days: dict[int, dict[str, pd.DataFrame]] = {}
    benchmark_by_days: dict[int, pd.Series] = {}
    for days in days_grid:
        histories = load_histories(
            days=days,
            interval=args.interval,
            csv_by_pair={
                "ETH/USD": args.eth_csv,
                "SOL/USD": args.sol_csv,
                "BTC/USD": args.btc_csv,
            },
        )
        histories_by_days[days] = histories
        benchmark_by_days[days] = build_benchmark_returns(histories)

    trials: list[dict] = []
    horizon_rows: list[dict] = []

    best_score = None
    best_payload = None

    trial_num = 0

    # Baseline first
    baseline_modes = [("baseline_no_move", 0.01, 0.0, 1.0, 1.0)]

    variable_modes = list(
        itertools.product(
            ["buy_only", "sell_only", "both"],
            trigger_pcts,
            slopes,
            max_mults,
            min_mults,
        )
    )

    for mode, trigger_pct, slope, max_mult, min_mult in baseline_modes + variable_modes:
        trial_num += 1

        if min_mult <= 0:
            continue
        if max_mult < min_mult:
            continue
        if trigger_pct <= 0:
            continue
        if slope < 0:
            continue

        overrides = mode_to_overrides(
            mode,
            trigger_pct=trigger_pct,
            slope=slope,
            max_mult=max_mult,
            min_mult=min_mult,
        )

        per_horizon = {}
        scores = []

        for days in days_grid:
            full_label = f"full_{days}d"

            merged, weights, trades = simulate_portfolio(
                histories_by_days[days],
                dynamic=True,
                interval=args.interval,
                dynamic_overrides=overrides,
            )
            summary = window_summaries(merged, args.interval, full_label=full_label)
            upside = compute_upside_metrics(merged, benchmark_by_days[days])
            horizon_score = score_one_horizon(summary, upside, full_label)

            per_horizon[days] = {
                "summary": summary,
                "upside": upside,
                "score": horizon_score,
                "merged": merged,
                "weights": weights,
                "trades": trades,
            }
            scores.append(horizon_score)

            horizon_rows.append(
                {
                    "trial": trial_num,
                    "mode": mode,
                    "days": days,
                    "trigger_pct": trigger_pct,
                    "slope": slope,
                    "max_mult": max_mult,
                    "min_mult": min_mult,
                    **overrides,
                    "score": horizon_score,
                    "full_return": summary[full_label]["total_return"],
                    "full_drawdown": summary[full_label]["max_drawdown"],
                    "trailing_21d_return": summary["trailing_21d"]["total_return"],
                    "trailing_21d_drawdown": summary["trailing_21d"]["max_drawdown"],
                    "trailing_10d_return": summary["trailing_10d"]["total_return"],
                    "trailing_10d_drawdown": summary["trailing_10d"]["max_drawdown"],
                    "trailing_10d_cash": summary["trailing_10d"]["avg_cash_weight"],
                    "trailing_21d_drift": summary["trailing_21d"]["avg_abs_weight_drift"],
                    "upside_capture_ratio": upside["upside_capture_ratio"],
                    "downside_capture_ratio": upside["downside_capture_ratio"],
                    "capture_spread": upside["upside_capture_ratio"] - upside["downside_capture_ratio"],
                }
            )

        aggregate_score = float(sum(scores) / len(scores))

        trial_row = {
            "trial": trial_num,
            "mode": mode,
            "trigger_pct": trigger_pct,
            "slope": slope,
            "max_mult": max_mult,
            "min_mult": min_mult,
            "aggregate_score": aggregate_score,
        }

        trial_row["avg_full_return"] = float(
            sum(per_horizon[d]["summary"][f"full_{d}d"]["total_return"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_full_drawdown"] = float(
            sum(per_horizon[d]["summary"][f"full_{d}d"]["max_drawdown"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_trailing_21d_return"] = float(
            sum(per_horizon[d]["summary"]["trailing_21d"]["total_return"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_trailing_10d_return"] = float(
            sum(per_horizon[d]["summary"]["trailing_10d"]["total_return"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_upside_capture_ratio"] = float(
            sum(per_horizon[d]["upside"]["upside_capture_ratio"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_downside_capture_ratio"] = float(
            sum(per_horizon[d]["upside"]["downside_capture_ratio"] for d in days_grid) / len(days_grid)
        )
        trial_row["avg_capture_spread"] = (
            trial_row["avg_upside_capture_ratio"] - trial_row["avg_downside_capture_ratio"]
        )

        trials.append(trial_row)

        if best_score is None or aggregate_score > best_score:
            best_score = aggregate_score
            best_payload = {
                "mode": mode,
                "trigger_pct": trigger_pct,
                "slope": slope,
                "max_mult": max_mult,
                "min_mult": min_mult,
                "overrides": overrides,
                "aggregate_score": aggregate_score,
                "per_horizon": per_horizon,
            }

    all_trials = pd.DataFrame(trials).sort_values("aggregate_score", ascending=False).reset_index(drop=True)
    top_trials = all_trials.head(30).copy()
    horizon_results = pd.DataFrame(horizon_rows).sort_values("score", ascending=False).reset_index(drop=True)

    all_trials.to_csv(outdir / "all_trials.csv", index=False)
    top_trials.to_csv(outdir / "top_trials.csv", index=False)
    horizon_results.to_csv(outdir / "horizon_results.csv", index=False)

    with open(outdir / "best_params.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "mode": best_payload["mode"],
                "trigger_pct": best_payload["trigger_pct"],
                "slope": best_payload["slope"],
                "max_mult": best_payload["max_mult"],
                "min_mult": best_payload["min_mult"],
                "overrides": best_payload["overrides"],
                "aggregate_score": best_payload["aggregate_score"],
            },
            f,
            indent=2,
        )

    best_horizon_summaries = {
        f"{days}d": best_payload["per_horizon"][days]["summary"] for days in days_grid
    }
    with open(outdir / "best_horizon_summaries.json", "w", encoding="utf-8") as f:
        json.dump(best_horizon_summaries, f, indent=2)

    best_upside_by_horizon = {
        f"{days}d": best_payload["per_horizon"][days]["upside"] for days in days_grid
    }
    with open(outdir / "best_upside_by_horizon.json", "w", encoding="utf-8") as f:
        json.dump(best_upside_by_horizon, f, indent=2)

    # Save winner artifacts by horizon
    for days in days_grid:
        payload = best_payload["per_horizon"][days]
        write_outputs(
            outdir / f"best_{days}d",
            f"best_mode_compare_{days}d",
            payload["merged"],
            payload["weights"],
            payload["trades"],
            dynamic=True,
            interval=args.interval,
            dynamic_overrides=best_payload["overrides"],
            full_label=f"full_{days}d",
        )

    lines = [
        "# Dynamic Mode Compare Sweep",
        "",
        "## Best Trial",
        f"- mode: {best_payload['mode']}",
        f"- trigger_pct: {best_payload['trigger_pct']}",
        f"- slope: {best_payload['slope']}",
        f"- max_mult: {best_payload['max_mult']}",
        f"- min_mult: {best_payload['min_mult']}",
        f"- aggregate_score: {best_payload['aggregate_score']}",
        "",
    ]

    for days in days_grid:
        summary = best_payload["per_horizon"][days]["summary"]
        upside = best_payload["per_horizon"][days]["upside"]
        full_label = f"full_{days}d"

        lines.append(f"## {days}d Horizon")
        lines.append(f"- horizon_score: {best_payload['per_horizon'][days]['score']}")
        lines.append("")
        lines.append(f"### {full_label}")
        for k, v in summary[full_label].items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("### trailing_21d")
        for k, v in summary["trailing_21d"].items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("### trailing_10d")
        for k, v in summary["trailing_10d"].items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("### upside_metrics")
        for k, v in upside.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")