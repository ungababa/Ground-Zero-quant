import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import yfinance as yf
from config import GridConfig, PairRules
from metrics import summarize_equity_curve
from strategy import GridStrategy, TickerView

FEE_RATE = 0.05 / 100


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            column[0] if isinstance(column, tuple) else column for column in df.columns
        ]
    df = df.reset_index().rename(
        columns={
            "index": "timestamp",
            "Datetime": "timestamp",
            "Date": "timestamp",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
        }
    )
    if "timestamp" not in df.columns:
        df = df.rename(columns={df.columns[0]: "timestamp"})
    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="raise")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return (
        df[["timestamp", "open", "high", "low", "close"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def load_history(
    days: int, csv_path: str | None = None, interval: str = "1h"
) -> pd.DataFrame:
    if csv_path:
        df = pd.read_csv(csv_path)
        return _normalize_history(df)
    df = yf.download(
        "BTC-USD",
        period=f"{days}d",
        interval=interval,
        auto_adjust=False,
        progress=False,
    )
    if df.empty:
        raise ValueError(
            "No historical data returned from yfinance for BTC-USD. "
            "Provide --csv with explicit OHLC data or investigate Yahoo/yfinance availability."
        )
    return _normalize_history(df)


def _orders_to_json(orders: list) -> str:
    return json.dumps(
        [
            {"side": order.side, "price": order.price, "quantity": order.quantity}
            for order in orders
        ],
        separators=(",", ":"),
    )


def _update_average_cost(
    avg_cost: float,
    current_btc: float,
    buy_qty: float,
    buy_price: float,
    buy_fee: float,
) -> float:
    effective_buy_cost = buy_price + (buy_fee / buy_qty)
    if current_btc <= 0:
        return effective_buy_cost
    return ((avg_cost * current_btc) + (effective_buy_cost * buy_qty)) / (
        current_btc + buy_qty
    )


def simulate_backtest(
    config: GridConfig, history: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    rules = PairRules(
        pair=config.pair, price_precision=2, amount_precision=6, min_order_value=1.0
    )
    strategy = GridStrategy(config, rules)
    initial_price = float(history.iloc[0].close)
    target_notional = config.max_position_notional_usd / 2.0
    initial_qty = target_notional / initial_price
    initial_fee = target_notional * FEE_RATE
    
    starting_cash = 50_000.0
    cash = starting_cash - target_notional - initial_fee
    btc = initial_qty
    avg_cost = initial_price
    realized_pnl = 0.0
    trades: list[dict] = [{
        "timestamp": history.iloc[0].timestamp,
        "side": "BUY",
        "price": initial_price,
        "quantity": initial_qty,
        "fee": initial_fee
    }]
    equity_rows: list[dict] = []
    detail_rows: list[dict] = []

    for row in history.itertuples(index=False):
        close = float(row.close)
        low = float(row.low)
        high = float(row.high)
        ticker = TickerView(bid=close, ask=close, last=close, change_24h=0.0)
        refresh_triggered = strategy.anchor_price is None or strategy.should_refresh(
            ticker
        )
        if refresh_triggered:
            strategy.set_anchor(ticker.mid)

        desired = strategy.desired_orders(ticker, btc)
        fills_this_bar: list[dict] = []

        for order in desired:
            filled = (
                order.side == "BUY"
                and low <= order.price
                or order.side == "SELL"
                and high >= order.price
            )
            if not filled:
                continue
            notional = order.price * order.quantity
            fee = notional * FEE_RATE
            if order.side == "BUY" and cash >= notional + fee:
                avg_cost = _update_average_cost(
                    avg_cost, btc, order.quantity, order.price, fee
                )
                cash -= notional + fee
                btc += order.quantity
                fill = {
                    "timestamp": row.timestamp,
                    "side": order.side,
                    "price": order.price,
                    "quantity": order.quantity,
                    "fee": fee,
                }
                trades.append(fill)
                fills_this_bar.append(fill)
                if config.reanchor_after_fill:
                    strategy.set_anchor(order.price)
            elif order.side == "SELL" and btc >= order.quantity:
                realized_pnl += (order.price - avg_cost) * order.quantity - fee
                cash += notional - fee
                btc -= order.quantity
                if btc == 0:
                    avg_cost = 0.0
                fill = {
                    "timestamp": row.timestamp,
                    "side": order.side,
                    "price": order.price,
                    "quantity": order.quantity,
                    "fee": fee,
                }
                trades.append(fill)
                fills_this_bar.append(fill)
                if config.reanchor_after_fill:
                    strategy.set_anchor(order.price)

        equity = cash + btc * close
        unrealized_pnl = btc * (close - avg_cost) if btc > 0 else 0.0
        next_orders = strategy.desired_orders(ticker, btc)
        bh_equity = (starting_cash / initial_price) * close

        equity_rows.append(
            {
                "timestamp": row.timestamp,
                "equity": equity,
                "bh_equity": bh_equity, # buy-and-hold equity for comparison
                "cash": cash,
                "btc": btc,
                "close": close,
            }
        )
        detail_rows.append(
            {
                "timestamp": row.timestamp,
                "open": float(row.open),
                "high": high,
                "low": low,
                "close": close,
                "anchor_price": strategy.anchor_price,
                "refresh_triggered": refresh_triggered,
                "current_position_btc": btc,
                "current_position_notional_usd": btc * close,
                "average_cost": avg_cost,
                "cash_usd": cash,
                "btc_market_value_usd": btc * close,
                "equity_usd": equity,
                "realized_pnl_usd": realized_pnl,
                "unrealized_pnl_usd": unrealized_pnl,
                "current_limit_orders_json": _orders_to_json(next_orders),
                "fill_count": len(fills_this_bar),
                "fills_json": json.dumps(
                    fills_this_bar, default=str, separators=(",", ":")
                ),
                "debug_max_position_notional_usd": config.max_position_notional_usd,
                "debug_spacing_pct": config.spacing_pct,
                "debug_levels_per_side": config.levels_per_side,
            }
        )

    return pd.DataFrame(equity_rows), trades, pd.DataFrame(detail_rows)


def run_backtest(
    config: GridConfig, history: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict]]:
    equity_curve, trades, _ = simulate_backtest(config, history)
    return equity_curve, trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--spacing-pct", type=float, default=0.003)
    parser.add_argument("--levels-per-side", type=int, default=6)
    parser.add_argument("--per-level-notional-usd", type=float, default=2000.0)
    parser.add_argument("--max-position-notional-usd", type=float, default=50000.0)
    parser.add_argument("--interval", type=str, default="1h")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = GridConfig(
        spacing_pct=args.spacing_pct,
        levels_per_side=args.levels_per_side,
        per_level_notional_usd=args.per_level_notional_usd,
        max_position_notional_usd=args.max_position_notional_usd,
    )
    history = load_history(args.days, args.csv, args.interval)
    equity_curve, trades, detail_log = simulate_backtest(config, history)
    summary = summarize_equity_curve(equity_curve)
    print({"config": asdict(config), "metrics": summary, "trades": len(trades)})
    out_dir = Path("artifacts")
    out_dir.mkdir(exist_ok=True)
    equity_curve.to_csv(out_dir / "equity_curve.csv", index=False)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    detail_log.to_csv(out_dir / "detailed_backtest_log.csv", index=False)
