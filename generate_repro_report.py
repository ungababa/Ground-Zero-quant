import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--sample-rows", type=int, default=20)
    parser.add_argument(
        "--pair", default="SOL/USD", help="Trading pair displayed in the report, e.g. SOL/USD or BTC/USD"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifacts_dir = Path(args.artifacts_dir)
    coin = args.pair.split("/")[0]
    detail = pd.read_csv(artifacts_dir / "detailed_backtest_log.csv")
    detail["timestamp"] = pd.to_datetime(detail["timestamp"], utc=True)
    equity = pd.read_csv(artifacts_dir / "equity_curve.csv")
    equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)

    equity_series = equity["equity"].astype(float)
    running_peak = equity_series.cummax()
    drawdown = equity_series / running_peak - 1.0

    fig, axes = plt.subplots(3, 1, figsize=(15, 12), sharex=True)
    axes[0].plot(
        detail["timestamp"],
        detail["current_position_coin"],
        color="purple",
        linewidth=1.5,
    )
    axes[0].set_title(f"{coin} Position Over Time")
    axes[0].set_ylabel(coin)
    axes[0].grid(alpha=0.25)

    axes[1].plot(
        detail["timestamp"],
        detail["unrealized_pnl_usd"],
        color="darkorange",
        linewidth=1.5,
    )
    axes[1].axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    axes[1].set_title("Unrealized PnL Over Time")
    axes[1].set_ylabel("USD")
    axes[1].grid(alpha=0.25)

    axes[2].plot(equity["timestamp"], drawdown, color="firebrick", linewidth=1.5)
    axes[2].fill_between(equity["timestamp"], drawdown, 0, color="firebrick", alpha=0.2)
    axes[2].set_title("Drawdown Curve")
    axes[2].set_ylabel("Drawdown")
    axes[2].set_xlabel("Time")
    axes[2].grid(alpha=0.25)

    fig.tight_layout()
    plot_path = artifacts_dir / "backtest_state_panels.png"
    fig.savefig(plot_path, dpi=180, bbox_inches="tight")

    columns = [
        "timestamp",
        "current_position_coin",
        "current_limit_orders_json",
        "unrealized_pnl_usd",
        "anchor_price",
        "refresh_triggered",
        "fill_count",
        "fills_json",
    ]
    sample = detail[columns].head(args.sample_rows).fillna("")
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = [
        "| " + " | ".join(str(row[column]).replace("\n", " ") for column in columns) + " |"
        for _, row in sample.iterrows()
    ]
    md_lines = [
        "# Backtest Reproduction Log",
        "",
        "This table is generated from `artifacts/detailed_backtest_log.csv`.",
        "It records per-bar simulation state from the actual 1h backtest.",
        "",
        header,
        separator,
        *rows,
        "",
        "## Files",
        "",
        f"- Full detailed log: `{artifacts_dir / 'detailed_backtest_log.csv'}`",
        f"- Extra state chart: `{plot_path}`",
        f"- Trade chart: `{artifacts_dir / 'backtest_trades.png'}`",
        f"- Trades: `{artifacts_dir / 'trades.csv'}`",
        f"- Equity: `{artifacts_dir / 'equity_curve.csv'}`",
    ]
    markdown_path = artifacts_dir / "reproduction_log.md"
    markdown_path.write_text("\n".join(md_lines), encoding="utf-8")

    manifest = {
        "detailed_log_csv": str(artifacts_dir / "detailed_backtest_log.csv"),
        "reproduction_markdown": str(markdown_path),
        "state_plot": str(plot_path),
    }
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
