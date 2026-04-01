from __future__ import annotations

import argparse
import sys
from pathlib import Path



ROOT = Path(__file__).resolve().parent.parent
print(f"Adding {ROOT} to sys.path")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_portfolio_engine import load_histories, simulate_portfolio, write_outputs

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--eth-csv", type=str, default=None)
    parser.add_argument("--sol-csv", type=str, default=None)
    parser.add_argument("--btc-csv", type=str, default=None)
    parser.add_argument("--outdir", type=str, default="artifacts_dynamic")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    histories = load_histories(
        days=args.days,
        interval=args.interval,
        csv_by_pair={"ETH/USD": args.eth_csv, "SOL/USD": args.sol_csv, "BTC/USD": args.btc_csv},
    )
    merged, weights, trades = simulate_portfolio(
        histories,
        dynamic=True,
        interval=args.interval,
    )
    write_outputs(
        args.outdir,
        "dynamic",
        merged,
        weights,
        trades,
        dynamic=True,
        interval=args.interval,
        full_label=f"full_{args.days}d",
    )