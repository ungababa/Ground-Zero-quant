import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from metrics import summarize_equity_curve

FORMULAS = {
    "roi": "ROI = (Ending Equity / Starting Equity) - 1",
    "period_return": "r_t = (Equity_t / Equity_{t-1}) - 1",
    "sharpe": "Sharpe = sqrt(N) * mean(r_t) / std(r_t)",
    "sortino": "Sortino = sqrt(N) * mean(r_t) / std(r_t for r_t < 0)",
    "drawdown": "Drawdown_t = (Equity_t / cummax(Equity)_t) - 1",
    "max_drawdown": "Max Drawdown = min(Drawdown_t)",
    "calmar": "Calmar = Annualized Return / abs(Max Drawdown)",
    "annualized_return": "Annualized Return = (1 + ROI)^(N / T) - 1",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", default="artifacts")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifacts_dir = Path(args.artifacts_dir)
    trades = pd.read_csv(artifacts_dir / "trades.csv")
    equity = pd.read_csv(artifacts_dir / "equity_curve.csv")

    trades["timestamp"] = pd.to_datetime(trades["timestamp"], utc=True)
    equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)

    buy_trades = trades[trades["side"] == "BUY"]
    sell_trades = trades[trades["side"] == "SELL"]

    bot_summary = summarize_equity_curve(equity)
    
    bh_summary = None
    if "bh_equity" in equity.columns:
        bh_summary = summarize_equity_curve(equity[["bh_equity"]].rename(columns={"bh_equity": "equity"}))

    trade_count = int(len(trades))

    fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

    axes[0].plot(
        equity["timestamp"],
        equity["close"],
        color="steelblue",
        linewidth=1.4,
        label="BTC Close",
    )
    axes[0].scatter(
        buy_trades["timestamp"],
        buy_trades["price"],
        color="green",
        s=30,
        label="BUY",
        alpha=0.85,
    )
    axes[0].scatter(
        sell_trades["timestamp"],
        sell_trades["price"],
        color="red",
        s=30,
        label="SELL",
        alpha=0.85,
    )
    axes[0].set_title("BTC/USD Grid Bot Backtest Trades")
    axes[0].set_ylabel("Price (USD)")
    axes[0].legend(loc="best")
    axes[0].grid(alpha=0.25)

    axes[1].plot(
        equity["timestamp"],
        equity["equity"],
        color="black",
        linewidth=1.6,
        label="Grid Bot Equity",
    )

    if "bh_equity" in equity.columns:
        axes[1].plot(
            equity["timestamp"],
            equity["bh_equity"],
            color="gray",
            linewidth=1.2,
            linestyle="--",
            label="Buy & Hold (Benchmark)",
        )
    axes[1].set_title("Equity Curve")
    axes[1].set_ylabel("Equity (USD)")
    axes[1].set_xlabel("Time")
    axes[1].grid(alpha=0.25)
    axes[1].legend(loc="best")

    if bh_summary:
        metrics_text = (
            f"{'Metric':<12} | {'Bot':<8} | {'B&H':<8}\n"
            f"{'-'*32}\n"
            f"{'ROI':<12} | {bot_summary['total_return']:>7.2%} | {bh_summary['total_return']:>7.2%}\n"
            f"{'Max DD':<12} | {bot_summary['max_drawdown']:>7.2%} | {bh_summary['max_drawdown']:>7.2%}\n"
            f"{'Sharpe':<12} | {bot_summary['sharpe']:>7.3f} | {bh_summary['sharpe']:>7.3f}\n"
            f"{'Sortino':<12} | {bot_summary['sortino']:>7.3f} | {bh_summary['sortino']:>7.3f}\n"
            f"{'Calmar':<12} | {bot_summary['calmar']:>7.3f} | {bh_summary['calmar']:>7.3f}\n"
            f"{'Trades':<12} | {trade_count:>7} | {'N/A':>7}"
        )
    else:
        # Fallback if B&H data is missing
        metrics_text = "\n".join([
            f"ROI: {bot_summary['total_return']:.2%}",
            f"Max DD: {bot_summary['max_drawdown']:.2%}",
            f"Sharpe: {bot_summary['sharpe']:.3f}",
            f"Trades: {trade_count}"
        ])

    axes[1].text(
        1.01,
        0.98,
        metrics_text,
        transform=axes[1].transAxes,
        va="top",
        ha="left",
        fontsize=10,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.9},
    )

    fig.tight_layout()
    output_png = artifacts_dir / "backtest_trades.png"
    fig.savefig(output_png, dpi=180, bbox_inches="tight")

    report = {
        "metrics": bot_summary,
        "bh_metrics": bh_summary,
        "formulas": FORMULAS,
        "files": {
            "trade_plot": str(output_png),
            "trades_csv": str(artifacts_dir / "trades.csv"),
            "equity_curve_csv": str(artifacts_dir / "equity_curve.csv"),
        },
    }
    with open(artifacts_dir / "metrics_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
