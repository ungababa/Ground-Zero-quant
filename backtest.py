import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
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
    days: int, csv_path: str | None = None, interval: str = "1h", ticker: str = "SOL-USD"
) -> pd.DataFrame:
    if csv_path:
        df = pd.read_csv(csv_path)
        return _normalize_history(df)
    df = yf.download(
        ticker,
        period=f"{days}d",
        interval=interval,
        auto_adjust=False,
        progress=False,
    )
    if df.empty:
        raise ValueError(
            f"No historical data returned from yfinance for {ticker}. "
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
    current_coin: float,
    buy_qty: float,
    buy_price: float,
    buy_fee: float,
) -> float:
    effective_buy_cost = buy_price + (buy_fee / buy_qty)
    if current_coin <= 0:
        return effective_buy_cost
    return ((avg_cost * current_coin) + (effective_buy_cost * buy_qty)) / (
        current_coin + buy_qty
    )


def _compute_change_24h(df: pd.DataFrame) -> np.ndarray:
    """Lookahead-free rolling 24-hour close-to-close price change.

    At bar i (already closed):
        change = (close[i] - close[ref]) / close[ref]
    where ref is the newest bar whose timestamp <= timestamp[i] - 24 hours.

    Returns NaN for bars that have fewer than 24 h of prior history, so the
    pause guard is never incorrectly triggered during the warm-up period.
    """
    ts = df["timestamp"].values.astype("datetime64[ns]")
    closes = df["close"].to_numpy(dtype=float)
    window = np.timedelta64(24, "h")
    changes = np.full(len(df), np.nan)
    for i in range(len(df)):
        cutoff = ts[i] - window
        # searchsorted gives the insertion point; step back one to get the
        # last bar whose timestamp is <= cutoff (strictly in the past).
        j = int(np.searchsorted(ts, cutoff, side="right")) - 1
        if j >= 0:
            changes[i] = (closes[i] - closes[j]) / closes[j]
    return changes


def simulate_backtest(
    config: GridConfig, history: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    rules = PairRules(
        pair=config.pair, price_precision=2, amount_precision=6, min_order_value=1.0
    )
    strategy = GridStrategy(config, rules)
    initial_price = float(history.iloc[0].open)
    starting_cash = 1_000_000.0
    target_notional = starting_cash * 0.5  # start 50% deployed in coin
    initial_qty = target_notional / initial_price
    initial_fee = target_notional * FEE_RATE

    cash = starting_cash - target_notional - initial_fee
    coin = initial_qty
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
    change_24h_arr = _compute_change_24h(history)
    # Shift by one bar: at bar-open we only know the *previous* bar's close-to-close
    # change. Using the current bar's close would be lookahead bias for the pause guard.
    change_24h_arr = np.concatenate([[np.nan], change_24h_arr[:-1]])

    for bar_idx, row in enumerate(history.itertuples(index=False)):
        open_price = float(row.open)
        close = float(row.close)
        low = float(row.low)
        high = float(row.high)
        raw_c24h = change_24h_arr[bar_idx]
        # NaN means <24 h of history — treat as no change so pause never fires
        # during warm-up (matches live-bot behaviour on first startup).
        c24h = float(raw_c24h) if not np.isnan(raw_c24h) else 0.0
        # Build the tick from the bar's open price — that is the only price known
        # when the bar begins. Using close here would be same-bar lookahead bias
        # because the close is only revealed after the bar ends.
        tick = TickerView(bid=open_price, ask=open_price, last=open_price, change_24h=c24h)

        paused = config.pause_guard and strategy.should_pause(tick)
        refresh_triggered = (not paused) and (
            strategy.anchor_price is None or strategy.should_refresh(tick)
        )
        if refresh_triggered:
            strategy.set_anchor(tick.mid)

        fills_this_bar: list[dict] = []
        desired: list = []

        if not paused:
            desired = strategy.desired_orders(tick, coin)

        for order in desired[: config.max_open_orders]:
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
                    avg_cost, coin, order.quantity, order.price, fee
                )
                cash -= notional + fee
                coin += order.quantity
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
            elif order.side == "SELL" and coin >= order.quantity:
                realized_pnl += (order.price - avg_cost) * order.quantity - fee
                cash += notional - fee
                coin -= order.quantity
                if coin == 0:
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

        equity = cash + coin * close
        unrealized_pnl = coin * (close - avg_cost) if coin > 0 else 0.0
        next_orders = [] if paused else strategy.desired_orders(tick, coin)
        bh_equity = (starting_cash / initial_price) * close

        equity_rows.append(
            {
                "timestamp": row.timestamp,
                "equity": equity,
                "bh_equity": bh_equity,  # buy-and-hold equity for comparison
                "cash": cash,
                "coin": coin,
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
                "current_position_coin": coin,
                "current_position_notional_usd": coin * close,
                "average_cost": avg_cost,
                "cash_usd": cash,
                "coin_market_value_usd": coin * close,
                "equity_usd": equity,
                "realized_pnl_usd": realized_pnl,
                "unrealized_pnl_usd": unrealized_pnl,
                "current_limit_orders_json": _orders_to_json(next_orders),
                "fill_count": len(fills_this_bar),
                "fills_json": json.dumps(
                    fills_this_bar, default=str, separators=(",", ":")
                ),
                "paused": paused,
                "change_24h": c24h,
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
    parser.add_argument("--pair", type=str, default="SOL/USD",
                        help="Trading pair, e.g. SOL/USD or BTC/USD")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--interval", type=str, default="1h")
    # Grid parameters — defaults mirror GridConfig / from_env() live defaults
    parser.add_argument("--spacing-pct", type=float, default=0.01)
    parser.add_argument("--levels-per-side", type=int, default=30)
    parser.add_argument("--per-level-notional-usd", type=float, default=10000)
    parser.add_argument("--max-position-notional-usd", type=float, default=math.inf,
                        help="Max notional USD of coin held. Defaults to unlimited.")
    parser.add_argument("--refresh-threshold-pct", type=float, default=0.05)
    parser.add_argument("--max-open-orders", type=int, default=1000)
    parser.add_argument("--max-24h-abs-change-pct", type=float, default=0.05)
    parser.add_argument(
        "--reanchor-after-fill",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--pause-guard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable the 24h-change volatility pause guard (default: enabled). "
             "Use --no-pause-guard to run the bot through all market conditions.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    yf_ticker = args.pair.replace("/", "-")  # "SOL/USD" -> "SOL-USD"
    config = GridConfig(
        pair=args.pair,
        spacing_pct=args.spacing_pct,
        levels_per_side=args.levels_per_side,
        per_level_notional_usd=args.per_level_notional_usd,
        max_position_notional_usd=args.max_position_notional_usd,
        refresh_threshold_pct=args.refresh_threshold_pct,
        max_open_orders=args.max_open_orders,
        max_24h_abs_change_pct=args.max_24h_abs_change_pct,
        reanchor_after_fill=args.reanchor_after_fill,
        pause_guard=args.pause_guard,
    )
    history = load_history(args.days, args.csv, args.interval, yf_ticker)
    equity_curve, trades, detail_log = simulate_backtest(config, history)
    summary = summarize_equity_curve(equity_curve)
    print({"config": asdict(config), "metrics": summary, "trades": len(trades)})
    out_dir = Path("artifacts")
    out_dir.mkdir(exist_ok=True)
    equity_curve.to_csv(out_dir / "equity_curve.csv", index=False)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    detail_log.to_csv(out_dir / "detailed_backtest_log.csv", index=False)
