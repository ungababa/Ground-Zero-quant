from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import GridConfig, PairRules
from src.strategy import GridStrategy, TickerView

FEE_RATE = 0.05 / 100
PAIRS = ("ETH/USD", "SOL/USD", "BTC/USD")
YF_TICKERS = {"ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD", "BTC/USD": "BTC-USD"}
TARGET_WEIGHTS = {"ETH/USD": 0.50, "SOL/USD": 0.25, "BTC/USD": 0.25}


class SimpleRuntime:
    def __init__(
        self,
        current_qty: float,
        deployable_cash: float,
        buy_notional: float,
        sell_notional: float,
        max_position: float,
    ):
        self.current_qty = current_qty
        self.deployable_cash = deployable_cash
        self.buy_notional = buy_notional
        self.sell_notional = sell_notional
        self.max_position = max_position


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


def load_history(days: int, interval: str, csv_path: str | None, ticker: str) -> pd.DataFrame:
    if csv_path:
        return _normalize_history(pd.read_csv(csv_path))

    import yfinance as yf

    df = yf.download(ticker, period=f"{days}d", interval=interval, auto_adjust=False, progress=False)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    return _normalize_history(df)


def load_histories(days: int, interval: str, csv_by_pair: dict[str, str | None]) -> dict[str, pd.DataFrame]:
    histories = {pair: load_history(days, interval, csv_by_pair.get(pair), YF_TICKERS[pair]) for pair in PAIRS}
    timestamps = set(histories[PAIRS[0]]["timestamp"])
    for pair in PAIRS[1:]:
        timestamps &= set(histories[pair]["timestamp"])

    keep = pd.Series(sorted(timestamps))
    out: dict[str, pd.DataFrame] = {}
    for pair, df in histories.items():
        out[pair] = df[df["timestamp"].isin(keep)].sort_values("timestamp").reset_index(drop=True)
    return out


def compute_change_24h(df: pd.DataFrame) -> np.ndarray:
    ts = df["timestamp"].values.astype("datetime64[ns]")
    closes = df["close"].to_numpy(dtype=float)
    window = np.timedelta64(24, "h")
    changes = np.full(len(df), np.nan)
    for i in range(len(df)):
        cutoff = ts[i] - window
        j = int(np.searchsorted(ts, cutoff, side="right")) - 1
        if j >= 0:
            changes[i] = (closes[i] - closes[j]) / closes[j]
    return np.nan_to_num(changes, nan=0.0)


def max_drawdown(equity: pd.Series) -> float:
    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    return float(drawdown.min())


def summarize_window(df: pd.DataFrame, bars_per_year: int) -> dict[str, float | dict[str, float]]:
    equity = df["equity"].astype(float)
    returns = equity.pct_change().fillna(0.0)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    dd = max_drawdown(equity)
    vol = float(returns.std(ddof=0) * np.sqrt(bars_per_year)) if len(returns) > 1 else 0.0
    sharpe = float(np.sqrt(bars_per_year) * returns.mean() / returns.std(ddof=0)) if returns.std(ddof=0) > 0 else 0.0
    downside = returns[returns < 0]
    sortino = float(np.sqrt(bars_per_year) * returns.mean() / downside.std(ddof=0)) if len(downside) > 1 and downside.std(ddof=0) > 0 else 0.0
    annualized = float((1.0 + total_return) ** (bars_per_year / max(len(df), 1)) - 1.0) if len(df) > 1 else total_return
    calmar = float(annualized / abs(dd)) if dd != 0 else 0.0

    return {
        "total_return": total_return,
        "max_drawdown": dd,
        "annualized_vol": vol,
        "sharpe_like": sharpe,
        "sortino_like": sortino,
        "calmar_like": calmar,
        "avg_cash_weight": float(df["cash_weight"].mean()),
        "median_cash_weight": float(df["cash_weight"].median()),
        "avg_deployed_weight": float((1.0 - df["cash_weight"]).mean()),
        "avg_btc_weight": float(df["btc_weight"].mean()),
        "avg_abs_weight_drift": float(df["abs_weight_drift"].mean()),
        "positive_bar_rate": float((returns > 0).mean()),
        "final_weights": {
            "eth": float(df["eth_weight"].iloc[-1]),
            "sol": float(df["sol_weight"].iloc[-1]),
            "btc": float(df["btc_weight"].iloc[-1]),
            "cash": float(df["cash_weight"].iloc[-1]),
        },
    }


def window_summaries(df: pd.DataFrame, interval: str, full_label: str = "full_60d") -> dict[str, dict]:
    bars_per_day = 24 if interval.endswith("h") else 1
    bars_per_year = bars_per_day * 365
    windows = {
        full_label: df,
        "first_21d": df.iloc[: min(len(df), 21 * bars_per_day)].copy(),
        "trailing_21d": df.iloc[max(0, len(df) - 21 * bars_per_day):].copy(),
        "trailing_10d": df.iloc[max(0, len(df) - 10 * bars_per_day):].copy(),
    }
    return {name: summarize_window(window, bars_per_year) for name, window in windows.items() if len(window) > 1}


def make_configs(dynamic: bool) -> dict[str, GridConfig]:
    configs: dict[str, GridConfig] = {}

    for pair in PAIRS:
        weight = TARGET_WEIGHTS[pair]

        if dynamic:
            cfg = GridConfig(
                pair=pair,
                levels_per_side=25,
                spacing_pct=0.011,
                buy_spacing_multiplier=1.0,
                sell_spacing_multiplier=1.0,
                base_per_level_notional_usd=(1_000_000.0 * weight) * 0.022,
                base_max_position_notional_usd=1_000_000.0 * weight,
                per_level_notional_usd=(1_000_000.0 * weight) * 0.022,
                max_position_notional_usd=1_000_000.0 * weight,
                target_weight_pct=weight,
                order_size_pct_of_target=0.022,
                max_position_pct_of_target=1.0,
                min_inventory_floor_pct_of_target=0.10,
                adaptive_order_levels=10,
                refresh_threshold_pct=0.05,
                cash_reserve_pct=0.07,
                max_open_orders=64,
                max_24h_abs_change_pct=0.12,
                reanchor_after_fill=True,
                pause_guard=True,
                enable_signal_tilt=True,
                signal_spacing_tilt_pct=0.05,
            )
        else:
            cfg = GridConfig(
                pair=pair,
                levels_per_side=35,
                spacing_pct=0.011,
                buy_spacing_multiplier=1.2,
                sell_spacing_multiplier=1.0,
                base_per_level_notional_usd=(1_000_000.0 * weight) * 0.017,
                base_max_position_notional_usd=1_000_000.0 * weight,
                per_level_notional_usd=(1_000_000.0 * weight) * 0.017,
                max_position_notional_usd=1_000_000.0 * weight,
                target_weight_pct=0.0,
                order_size_pct_of_target=0.0,
                max_position_pct_of_target=0.0,
                min_inventory_floor_pct_of_target=0.20,
                adaptive_order_levels=6,
                refresh_threshold_pct=0.05,
                cash_reserve_pct=0.05,
                capital_base_usd=1_000_000.0 * weight,
                max_open_orders=64,
                max_24h_abs_change_pct=0.12,
                reanchor_after_fill=True,
                pause_guard=True,
                enable_signal_tilt=True,
                signal_spacing_tilt_pct=0.10,
            )

        configs[pair] = cfg

    return configs


def build_prev_runtime(config: GridConfig, cash: float, qty: float, price: float) -> SimpleRuntime:
    reserve = 1_000_000.0 * config.cash_reserve_pct
    wallet_deployable_cash = max(cash - reserve, 0.0)
    current_notional = qty * price
    bot_budget_remaining = max(config.capital_base_usd - current_notional - reserve, 0.0)
    deployable_cash = min(wallet_deployable_cash, bot_budget_remaining)

    return SimpleRuntime(
        current_qty=qty,
        deployable_cash=deployable_cash,
        buy_notional=config.base_per_level_notional_usd,
        sell_notional=config.base_per_level_notional_usd,
        max_position=config.base_max_position_notional_usd,
    )


def build_dynamic_runtime(config: GridConfig, cash: float, qty_by_pair: dict[str, float], open_prices: dict[str, float], pair: str) -> SimpleRuntime:
    total_equity = cash + sum(qty_by_pair[p] * open_prices[p] for p in PAIRS)
    target_notional = total_equity * config.target_weight_pct
    current_notional = qty_by_pair[pair] * open_prices[pair]
    reserve = total_equity * config.cash_reserve_pct

    investable_cap = target_notional * config.max_position_pct_of_target
    buy_gap = max(investable_cap - current_notional, 0.0)
    sell_gap = max(current_notional - target_notional * config.min_inventory_floor_pct_of_target, 0.0)

    preferred = target_notional * config.order_size_pct_of_target
    levels = max(config.adaptive_order_levels, 1)

    return SimpleRuntime(
        current_qty=qty_by_pair[pair],
        deployable_cash=max(cash - reserve, 0.0),
        buy_notional=min(preferred, buy_gap / levels) if buy_gap > 0 else 0.0,
        sell_notional=min(preferred, sell_gap / levels) if sell_gap > 0 else 0.0,
        max_position=investable_cap,
    )


def simulate_portfolio(
    histories: dict[str, pd.DataFrame],
    *,
    dynamic: bool,
    interval: str,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    configs = make_configs(dynamic)
    rules = {pair: PairRules(pair=pair, price_precision=2, amount_precision=6, min_order_value=1.0) for pair in PAIRS}
    strategies = {pair: GridStrategy(configs[pair], rules[pair]) for pair in PAIRS}
    changes = {pair: compute_change_24h(histories[pair]) for pair in PAIRS}

    initial_equity = 1_000_000.0
    reserve = initial_equity * configs[PAIRS[0]].cash_reserve_pct
    investable = initial_equity - reserve
    cash = reserve
    qty_by_pair = {pair: investable * TARGET_WEIGHTS[pair] / float(histories[pair].iloc[0]["open"]) for pair in PAIRS}
    avg_cost = {pair: float(histories[pair].iloc[0]["open"]) for pair in PAIRS}

    trades: list[dict] = []
    equity_rows: list[dict] = []
    weight_rows: list[dict] = []

    for i in range(len(histories[PAIRS[0]])):
        timestamp = histories[PAIRS[0]].iloc[i]["timestamp"]
        opens = {pair: float(histories[pair].iloc[i]["open"]) for pair in PAIRS}
        highs = {pair: float(histories[pair].iloc[i]["high"]) for pair in PAIRS}
        lows = {pair: float(histories[pair].iloc[i]["low"]) for pair in PAIRS}
        closes = {pair: float(histories[pair].iloc[i]["close"]) for pair in PAIRS}

        for pair in PAIRS:
            ticker = TickerView(
                bid=opens[pair],
                ask=opens[pair],
                last=opens[pair],
                change_24h=float(changes[pair][i]),
            )

            strategies[pair].record_price(ticker.mid)

            if configs[pair].pause_guard and strategies[pair].should_pause(ticker):
                continue

            if strategies[pair].should_refresh(ticker):
                strategies[pair].set_anchor(ticker.mid)

            runtime = (
                build_dynamic_runtime(configs[pair], cash, qty_by_pair, opens, pair)
                if dynamic
                else build_prev_runtime(configs[pair], cash, qty_by_pair[pair], opens[pair])
            )

            desired = strategies[pair].desired_orders(
                ticker=ticker,
                coin_position=runtime.current_qty,
                usd_free=runtime.deployable_cash,
                buy_order_notional_usd=runtime.buy_notional,
                sell_order_notional_usd=runtime.sell_notional,
                max_position_notional_usd=runtime.max_position,
            )

            for order in desired[: configs[pair].max_open_orders]:
                filled = (order.side == "BUY" and lows[pair] <= order.price) or (order.side == "SELL" and highs[pair] >= order.price)
                if not filled:
                    continue

                notional = order.price * order.quantity
                fee = notional * FEE_RATE

                if order.side == "BUY" and cash >= notional + fee:
                    total_cost = avg_cost[pair] * qty_by_pair[pair] + notional + fee
                    qty_by_pair[pair] += order.quantity
                    avg_cost[pair] = total_cost / qty_by_pair[pair]
                    cash -= notional + fee
                    trades.append(
                        {
                            "timestamp": timestamp,
                            "pair": pair,
                            "side": "BUY",
                            "price": order.price,
                            "quantity": order.quantity,
                            "fee": fee,
                        }
                    )
                    if configs[pair].reanchor_after_fill:
                        strategies[pair].set_anchor(order.price)

                elif order.side == "SELL" and qty_by_pair[pair] >= order.quantity:
                    qty_by_pair[pair] -= order.quantity
                    cash += notional - fee
                    trades.append(
                        {
                            "timestamp": timestamp,
                            "pair": pair,
                            "side": "SELL",
                            "price": order.price,
                            "quantity": order.quantity,
                            "fee": fee,
                        }
                    )
                    if qty_by_pair[pair] <= 0:
                        qty_by_pair[pair] = 0.0
                        avg_cost[pair] = 0.0
                    if configs[pair].reanchor_after_fill:
                        strategies[pair].set_anchor(order.price)

        equity = cash + sum(qty_by_pair[pair] * closes[pair] for pair in PAIRS)
        weights = {pair: (qty_by_pair[pair] * closes[pair] / equity if equity > 0 else 0.0) for pair in PAIRS}
        cash_weight = cash / equity if equity > 0 else 0.0
        drift = sum(abs(weights[pair] - TARGET_WEIGHTS[pair]) for pair in PAIRS)

        equity_rows.append({"timestamp": timestamp, "equity": equity, "cash": cash})
        weight_rows.append(
            {
                "timestamp": timestamp,
                "eth_weight": weights["ETH/USD"],
                "sol_weight": weights["SOL/USD"],
                "btc_weight": weights["BTC/USD"],
                "cash_weight": cash_weight,
                "abs_weight_drift": drift,
            }
        )

    equity_df = pd.DataFrame(equity_rows)
    weight_df = pd.DataFrame(weight_rows)
    merged = equity_df.merge(weight_df, on="timestamp", how="inner")
    return merged, weight_df, trades


def write_outputs(
    outdir: str | Path,
    label: str,
    merged: pd.DataFrame,
    weight_df: pd.DataFrame,
    trades: list[dict],
    dynamic: bool,
    interval: str,
    full_label: str = "full_60d",
) -> dict:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    merged.to_csv(outdir / f"{label}_equity_curve.csv", index=False)
    weight_df.to_csv(outdir / f"{label}_weights.csv", index=False)
    pd.DataFrame(trades).to_csv(outdir / f"{label}_trades.csv", index=False)

    summary = window_summaries(merged, interval, full_label=full_label)
    with open(outdir / "windowed_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(outdir / "run_config.json", "w", encoding="utf-8") as f:
        json.dump({"dynamic": dynamic, "label": label, "full_label": full_label}, f, indent=2)

    with open(outdir / "bot_profile.json", "w", encoding="utf-8") as f:
        json.dump({pair: asdict(cfg) for pair, cfg in make_configs(dynamic).items()}, f, indent=2)

    lines = [f"# {label}", ""]
    for window, metrics in summary.items():
        lines.append(f"## {window}")
        for k, v in metrics.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary