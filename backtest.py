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
        df.columns = [column[0] if isinstance(column, tuple) else column for column in df.columns]
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
    return df[["timestamp", "open", "high", "low", "close"]].sort_values("timestamp").reset_index(drop=True)


def load_history(days: int, csv_path: str | None = None, interval: str = "1h", ticker: str = "SOL-USD") -> pd.DataFrame:
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
        [{"side": order.side, "price": order.price, "quantity": order.quantity} for order in orders],
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
    return ((avg_cost * current_coin) + (effective_buy_cost * buy_qty)) / (current_coin + buy_qty)


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


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    if not weights:
        raise ValueError("weights cannot be empty")
    total = sum(float(value) for value in weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    return {key: float(value) / total for key, value in weights.items()}


def _merge_histories(histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if not histories:
        raise ValueError("histories cannot be empty")
    merged: pd.DataFrame | None = None
    required_cols = ["timestamp", "open", "high", "low", "close"]

    for symbol, df in histories.items():
        missing = [column for column in required_cols if column not in df.columns]
        if missing:
            raise ValueError(f"History for {symbol} is missing required columns: {missing}")
        symbol_upper = symbol.upper()
        renamed = df[required_cols].copy().rename(
            columns={
                "open": f"open_{symbol_upper}",
                "high": f"high_{symbol_upper}",
                "low": f"low_{symbol_upper}",
                "close": f"close_{symbol_upper}",
            }
        )
        merged = renamed if merged is None else merged.merge(renamed, on="timestamp", how="inner")

    if merged is None or merged.empty:
        raise ValueError("No overlapping timestamps found across supplied histories")

    return merged.sort_values("timestamp").reset_index(drop=True)


def simulate_backtest(config: GridConfig, history: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    rules = PairRules(pair=config.pair, price_precision=2, amount_precision=6, min_order_value=1.0)
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
    trades: list[dict] = [
        {
            "timestamp": history.iloc[0].timestamp,
            "side": "BUY",
            "price": initial_price,
            "quantity": initial_qty,
            "fee": initial_fee,
        }
    ]
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
        refresh_triggered = (not paused) and (strategy.anchor_price is None or strategy.should_refresh(tick))
        if refresh_triggered:
            strategy.set_anchor(tick.mid)

        fills_this_bar: list[dict] = []
        desired: list = []

        if not paused:
            desired = strategy.desired_orders(tick, coin)

        for order in desired[: config.max_open_orders]:
            filled = order.side == "BUY" and low <= order.price or order.side == "SELL" and high >= order.price
            if not filled:
                continue
            notional = order.price * order.quantity
            fee = notional * FEE_RATE
            if order.side == "BUY" and cash >= notional + fee:
                avg_cost = _update_average_cost(avg_cost, coin, order.quantity, order.price, fee)
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
                "fills_json": json.dumps(fills_this_bar, default=str, separators=(",", ":")),
                "paused": paused,
                "change_24h": c24h,
                "debug_max_position_notional_usd": config.max_position_notional_usd,
                "debug_spacing_pct": config.spacing_pct,
                "debug_levels_per_side": config.levels_per_side,
            }
        )

    return pd.DataFrame(equity_rows), trades, pd.DataFrame(detail_rows)


def run_backtest(config: GridConfig, history: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    equity_curve, trades, _ = simulate_backtest(config, history)
    return equity_curve, trades


def simulate_weighted_dual_backtest(
    configs: dict[str, GridConfig],
    histories: dict[str, pd.DataFrame],
    weights: dict[str, float],
    starting_cash: float = 1_000_000.0,
    invested_ratio: float = 0.5,
) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    symbols = sorted(configs.keys())
    if len(symbols) < 2:
        raise ValueError("At least two symbols are required for weighted dual backtesting")
    if set(symbols) != set(histories.keys()) or set(symbols) != set(weights.keys()):
        raise ValueError("configs, histories, and weights must contain identical symbol keys")
    if invested_ratio <= 0 or invested_ratio > 1:
        raise ValueError("invested_ratio must be in (0, 1]")

    norm_weights = _normalize_weights(weights)
    merged = _merge_histories(histories)

    strategies: dict[str, GridStrategy] = {}
    states: dict[str, dict[str, float]] = {}
    change_by_symbol: dict[str, pd.Series] = {}

    for symbol in symbols:
        config = configs[symbol]
        rules = PairRules(pair=config.pair, price_precision=2, amount_precision=6, min_order_value=1.0)
        strategies[symbol] = GridStrategy(config, rules)

        history = histories[symbol]
        shifted_change = np.concatenate([[np.nan], _compute_change_24h(history)[:-1]])
        change_by_symbol[symbol] = pd.Series(shifted_change, index=history["timestamp"])

    invested_cash = starting_cash * invested_ratio
    cash = starting_cash
    trades: list[dict] = []
    total_initial_fee = 0.0

    first_row = merged.iloc[0]
    for symbol in symbols:
        open_price = float(first_row[f"open_{symbol.upper()}"])
        target_notional = invested_cash * norm_weights[symbol]
        initial_qty = target_notional / open_price
        initial_fee = target_notional * FEE_RATE
        total_initial_fee += initial_fee

        cash -= target_notional + initial_fee
        states[symbol] = {
            "coin": initial_qty,
            "avg_cost": open_price,
            "realized_pnl": 0.0,
            "bh_qty": initial_qty,
        }
        trades.append(
            {
                "timestamp": first_row.timestamp,
                "asset": symbol,
                "side": "BUY",
                "price": open_price,
                "quantity": initial_qty,
                "fee": initial_fee,
            }
        )

    bh_cash = starting_cash - invested_cash - total_initial_fee
    equity_rows: list[dict] = []
    detail_rows: list[dict] = []

    for row in merged.itertuples(index=False):
        fills_this_bar: list[dict] = []
        next_orders_per_asset: dict[str, list] = {}
        paused_flags: dict[str, bool] = {}
        unrealized_total = 0.0

        for symbol in symbols:
            config = configs[symbol]
            strategy = strategies[symbol]
            state = states[symbol]
            suffix = symbol.upper()

            open_price = float(getattr(row, f"open_{suffix}"))
            high = float(getattr(row, f"high_{suffix}"))
            low = float(getattr(row, f"low_{suffix}"))
            close = float(getattr(row, f"close_{suffix}"))

            raw_c24h = change_by_symbol[symbol].get(row.timestamp, np.nan)
            c24h = float(raw_c24h) if not np.isnan(raw_c24h) else 0.0
            tick = TickerView(bid=open_price, ask=open_price, last=open_price, change_24h=c24h)

            paused = config.pause_guard and strategy.should_pause(tick)
            paused_flags[symbol] = paused
            refresh_triggered = (not paused) and (strategy.anchor_price is None or strategy.should_refresh(tick))
            if refresh_triggered:
                strategy.set_anchor(tick.mid)

            desired = [] if paused else strategy.desired_orders(tick, state["coin"])
            for order in desired[: config.max_open_orders]:
                filled = order.side == "BUY" and low <= order.price or order.side == "SELL" and high >= order.price
                if not filled:
                    continue

                notional = order.price * order.quantity
                fee = notional * FEE_RATE
                if order.side == "BUY" and cash >= notional + fee:
                    state["avg_cost"] = _update_average_cost(
                        state["avg_cost"],
                        state["coin"],
                        order.quantity,
                        order.price,
                        fee,
                    )
                    cash -= notional + fee
                    state["coin"] += order.quantity
                    fill = {
                        "timestamp": row.timestamp,
                        "asset": symbol,
                        "side": order.side,
                        "price": order.price,
                        "quantity": order.quantity,
                        "fee": fee,
                    }
                    trades.append(fill)
                    fills_this_bar.append(fill)
                    if config.reanchor_after_fill:
                        strategy.set_anchor(order.price)
                elif order.side == "SELL" and state["coin"] >= order.quantity:
                    state["realized_pnl"] += (order.price - state["avg_cost"]) * order.quantity - fee
                    cash += notional - fee
                    state["coin"] -= order.quantity
                    if state["coin"] == 0:
                        state["avg_cost"] = 0.0
                    fill = {
                        "timestamp": row.timestamp,
                        "asset": symbol,
                        "side": order.side,
                        "price": order.price,
                        "quantity": order.quantity,
                        "fee": fee,
                    }
                    trades.append(fill)
                    fills_this_bar.append(fill)
                    if config.reanchor_after_fill:
                        strategy.set_anchor(order.price)

            next_orders_per_asset[symbol] = [] if paused else strategy.desired_orders(tick, state["coin"])
            unrealized_total += state["coin"] * (close - state["avg_cost"]) if state["coin"] > 0 else 0.0

        portfolio_value = cash
        bh_value = bh_cash
        per_asset_equity: dict[str, float] = {}
        per_asset_close: dict[str, float] = {}

        for symbol in symbols:
            close = float(getattr(row, f"close_{symbol.upper()}"))
            value = states[symbol]["coin"] * close
            per_asset_equity[symbol] = value
            per_asset_close[symbol] = close
            portfolio_value += value
            bh_value += states[symbol]["bh_qty"] * close

        equity_row = {
            "timestamp": row.timestamp,
            "equity": portfolio_value,
            "bh_equity": bh_value,
            "cash": cash,
        }
        detail_row = {
            "timestamp": row.timestamp,
            "equity_usd": portfolio_value,
            "bh_equity_usd": bh_value,
            "cash_usd": cash,
            "unrealized_pnl_usd": unrealized_total,
            "fill_count": len(fills_this_bar),
            "fills_json": json.dumps(fills_this_bar, default=str, separators=(",", ":")),
            "paused_any": any(paused_flags.values()),
        }

        for symbol in symbols:
            lower_symbol = symbol.lower()
            equity_row[f"{lower_symbol}_coin"] = states[symbol]["coin"]
            equity_row[f"{lower_symbol}_close"] = per_asset_close[symbol]
            equity_row[f"{lower_symbol}_value"] = per_asset_equity[symbol]

            detail_row[f"{lower_symbol}_coin"] = states[symbol]["coin"]
            detail_row[f"{lower_symbol}_close"] = per_asset_close[symbol]
            detail_row[f"{lower_symbol}_value_usd"] = per_asset_equity[symbol]
            detail_row[f"{lower_symbol}_avg_cost"] = states[symbol]["avg_cost"]
            detail_row[f"{lower_symbol}_realized_pnl_usd"] = states[symbol]["realized_pnl"]
            detail_row[f"{lower_symbol}_paused"] = paused_flags[symbol]
            detail_row[f"{lower_symbol}_orders_json"] = _orders_to_json(next_orders_per_asset[symbol])

        equity_rows.append(equity_row)
        detail_rows.append(detail_row)

    return pd.DataFrame(equity_rows), trades, pd.DataFrame(detail_rows)


def run_weighted_dual_backtest(
    configs: dict[str, GridConfig],
    histories: dict[str, pd.DataFrame],
    weights: dict[str, float],
    starting_cash: float = 1_000_000.0,
    invested_ratio: float = 0.5,
) -> tuple[pd.DataFrame, list[dict]]:
    equity_curve, trades, _ = simulate_weighted_dual_backtest(
        configs=configs,
        histories=histories,
        weights=weights,
        starting_cash=starting_cash,
        invested_ratio=invested_ratio,
    )
    return equity_curve, trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=str, default="SOL/USD", help="Trading pair, e.g. SOL/USD or BTC/USD")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--interval", type=str, default="1h")
    # Grid parameters — defaults mirror GridConfig / from_env() live defaults
    parser.add_argument("--spacing-pct", type=float, default=0.01)
    parser.add_argument("--levels-per-side", type=int, default=30)
    parser.add_argument("--per-level-notional-usd", type=float, default=10000)
    parser.add_argument(
        "--max-position-notional-usd",
        type=float,
        default=math.inf,
        help="Max notional USD of coin held. Defaults to unlimited.",
    )
    parser.add_argument("--refresh-threshold-pct", type=float, default=0.05)
    parser.add_argument("--max-open-orders", type=int, default=1000)
    parser.add_argument("--max-24h-abs-change-pct", type=float, default=0.05)
    parser.add_argument(
        "--reanchor-after-fill",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--pause-guard",
        action=argparse.BooleanOptionalAction,
        default=False,
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
