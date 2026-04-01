from __future__ import annotations

import argparse
import concurrent.futures as cf
import itertools
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.config import GridConfig, PairRules
from src.strategy import GridStrategy, TickerView

FEE_RATE = 0.1 / 100
PAIRS = ("ETH/USD", "SOL/USD", "BTC/USD")
YF_TICKERS = {"ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD", "BTC/USD": "BTC-USD"}
TARGET_WEIGHTS = {"ETH/USD": 0.50, "SOL/USD": 0.25, "BTC/USD": 0.25}


@dataclass(frozen=True, slots=True)
class SweepParams:
    order_size_pct_of_target: float
    cash_reserve_pct: float
    emergency_reserve_pct: float
    crash_trigger_pct: float
    portfolio_drawdown_trigger_pct: float
    emergency_release_frac: float
    emergency_release_slope: float
    adaptive_order_levels: int
    levels_per_side: int
    spacing_pct: float
    buy_spacing_multiplier: float
    sell_spacing_multiplier: float
    min_inventory_floor_pct_of_target: float
    inventory_room_power: float
    buy_room_min_mult: float
    buy_room_max_mult: float
    sell_room_min_mult: float
    sell_room_max_mult: float
    signal_spacing_tilt_pct: float
    signal_extra_levels_per_score: int

    def to_flat_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(slots=True)
class TrialArtifacts:
    params: SweepParams
    merged: pd.DataFrame
    weights: pd.DataFrame
    trades: list[dict]
    summaries: dict[str, dict]
    regime_metrics: dict[str, dict]
    aggregate_score: float


@dataclass(slots=True)
class DynamicRuntime:
    current_qty: float
    deployable_cash: float
    buy_notional: float
    sell_notional: float
    max_position: float
    emergency_release_fraction: float
    emergency_locked_cash: float
    base_reserve_cash: float
    current_notional: float
    target_notional: float
    buy_gap: float
    sell_gap: float


_WORKER_HISTORIES: dict[str, pd.DataFrame] | None = None
_WORKER_BENCH_RETS: pd.Series | None = None
_WORKER_INTERVAL: str | None = None
_WORKER_INITIAL_EQUITY: float | None = None
_WORKER_FULL_LABEL: str | None = None


def parse_float_list(value: str) -> list[float]:
    return [float(x.strip()) for x in value.split(",") if x.strip()]


def parse_int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _coerce_timestamp_series(ts: pd.Series) -> pd.Series:
    s = ts.copy()

    if pd.api.types.is_datetime64_any_dtype(s):
        return pd.to_datetime(s, utc=True, errors="coerce")

    if pd.api.types.is_numeric_dtype(s):
        vals = pd.to_numeric(s, errors="coerce")
        finite = vals[np.isfinite(vals)]
        if len(finite) == 0:
            raise ValueError("Timestamp column is numeric but contains no finite values")

        abs_med = float(np.nanmedian(np.abs(finite)))
        min_val = float(np.nanmin(finite))
        max_val = float(np.nanmax(finite))

        # Excel serial dates typically sit around 20k-60k.
        if 20_000 <= abs_med <= 80_000:
            origin = pd.Timestamp("1899-12-30", tz="UTC")
            out = origin + pd.to_timedelta(vals, unit="D")
            return pd.to_datetime(out, utc=True, errors="coerce")

        if abs_med >= 1e17:
            unit = "ns"
        elif abs_med >= 1e14:
            unit = "us"
        elif abs_med >= 1e11:
            unit = "ms"
        elif abs_med >= 1e9:
            unit = "s"
        else:
            raise ValueError(
                f"Numeric timestamp values do not look like epoch times: min={min_val}, max={max_val}"
            )
        return pd.to_datetime(vals, unit=unit, utc=True, errors="coerce")

    out = pd.to_datetime(s, utc=True, errors="coerce")
    if out.notna().sum() == 0:
        raise ValueError("Could not parse timestamp column")
    return out


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    df = df.copy()

    # If timestamps live in the index (common with yfinance), bring them into a column first.
    if "timestamp" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            idx_name = df.index.name if df.index.name else "timestamp"
            df = df.reset_index().rename(columns={idx_name: "timestamp"})
        elif str(df.index.dtype).startswith("datetime64"):
            df = df.reset_index().rename(columns={df.columns[0]: "timestamp"})

    rename_map = {
        "Datetime": "timestamp",
        "Date": "timestamp",
        "date": "timestamp",
        "datetime": "timestamp",
        "index": "timestamp",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
    }
    df = df.rename(columns=rename_map)
    df = df.loc[:, ~df.columns.duplicated()].copy()

    if "timestamp" not in df.columns:
        first_col = df.columns[0]
        sample = df[first_col]
        if pd.api.types.is_datetime64_any_dtype(sample):
            df = df.rename(columns={first_col: "timestamp"})
        else:
            parsed = pd.to_datetime(sample, utc=True, errors="coerce")
            if parsed.notna().sum() >= max(3, len(parsed) // 2):
                df = df.rename(columns={first_col: "timestamp"})
            else:
                raise ValueError(
                    "Could not find a timestamp column. Columns present: " + ", ".join(map(str, df.columns))
                )

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"Missing required OHLC column '{col}'")
        df[col] = pd.to_numeric(df[col], errors="raise")

    df["timestamp"] = _coerce_timestamp_series(df["timestamp"])
    out = (
        df[["timestamp", "open", "high", "low", "close"]]
        .dropna()
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"])
        .reset_index(drop=True)
    )
    if out.empty:
        raise ValueError("History is empty after normalization")
    return out


def load_history(days: int, interval: str, csv_path: str | None, ticker: str) -> pd.DataFrame:
    if csv_path:
        return _normalize_history(pd.read_csv(csv_path))

    import yfinance as yf

    df = yf.download(ticker, period=f"{days}d", interval=interval, auto_adjust=False, progress=False)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    return _normalize_history(df)


def load_histories(days: int, interval: str, csv_by_pair: dict[str, str | None]) -> dict[str, pd.DataFrame]:
    histories = {
        pair: load_history(days, interval, csv_by_pair.get(pair), YF_TICKERS[pair]).copy()
        for pair in PAIRS
    }

    for pair in PAIRS:
        histories[pair]["timestamp"] = pd.to_datetime(histories[pair]["timestamp"], utc=True)
        histories[pair] = (
            histories[pair][["timestamp", "open", "high", "low", "close"]]
            .dropna()
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"])
            .reset_index(drop=True)
        )

    merged = histories[PAIRS[0]][["timestamp"]].copy()
    for pair in PAIRS[1:]:
        merged = merged.merge(histories[pair][["timestamp"]], on="timestamp", how="inner")

    if merged.empty:
        sizes = {pair: int(len(histories[pair])) for pair in PAIRS}
        mins = {
            pair: (histories[pair]["timestamp"].min().isoformat() if len(histories[pair]) else None)
            for pair in PAIRS
        }
        maxs = {
            pair: (histories[pair]["timestamp"].max().isoformat() if len(histories[pair]) else None)
            for pair in PAIRS
        }
        raise ValueError(
            "No overlapping timestamps across ETH/SOL/BTC after alignment. "
            f"Row counts={sizes}, mins={mins}, maxs={maxs}"
        )

    keep = pd.Index(merged["timestamp"])
    aligned: dict[str, pd.DataFrame] = {}
    for pair in PAIRS:
        df = histories[pair][histories[pair]["timestamp"].isin(keep)].copy()
        df = df.sort_values("timestamp").reset_index(drop=True)
        if df.empty:
            raise ValueError(f"{pair} is empty after timestamp alignment")
        aligned[pair] = df

    lengths = {pair: len(df) for pair, df in aligned.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"Aligned histories still have mismatched lengths: {lengths}")

    return aligned


def compute_change_24h(df: pd.DataFrame) -> np.ndarray:
    ts = df["timestamp"].values.astype("datetime64[ns]")
    closes = df["close"].to_numpy(dtype=float)
    changes = np.zeros(len(df), dtype=float)
    window = np.timedelta64(24, "h")
    for i in range(len(df)):
        cutoff = ts[i] - window
        j = int(np.searchsorted(ts, cutoff, side="right")) - 1
        if j >= 0 and closes[j] > 0:
            changes[i] = (closes[i] - closes[j]) / closes[j]
    return changes


def bars_per_day_from_interval(interval: str) -> int:
    interval = interval.strip().lower()
    if interval.endswith("m"):
        minutes = int(interval[:-1])
        if minutes <= 0:
            raise ValueError(f"Invalid interval: {interval}")
        return max(int(round(24 * 60 / minutes)), 1)
    if interval.endswith("h"):
        hours = int(interval[:-1])
        if hours <= 0:
            raise ValueError(f"Invalid interval: {interval}")
        return max(int(round(24 / hours)), 1)
    if interval.endswith("d"):
        days = int(interval[:-1])
        if days <= 0:
            raise ValueError(f"Invalid interval: {interval}")
        return max(int(round(1 / days)), 1)
    return 24


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
    benchmark_return = float(df["benchmark_equity"].iloc[-1] / df["benchmark_equity"].iloc[0] - 1.0)

    return {
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": total_return - benchmark_return,
        "max_drawdown": dd,
        "annualized_vol": vol,
        "sharpe_like": sharpe,
        "sortino_like": sortino,
        "calmar_like": calmar,
        "avg_cash_weight": float(df["cash_weight"].mean()),
        "median_cash_weight": float(df["cash_weight"].median()),
        "avg_deployed_weight": float((1.0 - df["cash_weight"]).mean()),
        "avg_abs_weight_drift": float(df["abs_weight_drift"].mean()),
        "avg_release_fraction": float(df["release_fraction"].mean()),
        "avg_locked_emergency_cash": float(df["locked_emergency_cash"].mean()),
        "positive_bar_rate": float((returns > 0).mean()),
        "trade_count": int(df["bar_trade_count"].sum()),
        "buys": int(df["bar_buys"].sum()),
        "sells": int(df["bar_sells"].sum()),
        "trades_per_day": float(df["bar_trade_count"].sum() / max(df["bar_day_fraction"].sum(), 1e-9)),
        "final_weights": {
            "eth": float(df["eth_weight"].iloc[-1]),
            "sol": float(df["sol_weight"].iloc[-1]),
            "btc": float(df["btc_weight"].iloc[-1]),
            "cash": float(df["cash_weight"].iloc[-1]),
        },
    }


def build_window_summaries(df: pd.DataFrame, interval: str, full_label: str) -> dict[str, dict]:
    bars_per_day = bars_per_day_from_interval(interval)
    bars_per_year = bars_per_day * 365
    windows = {
        full_label: df,
        "trailing_21d": df.iloc[max(0, len(df) - 21 * bars_per_day):].copy(),
        "trailing_10d": df.iloc[max(0, len(df) - 10 * bars_per_day):].copy(),
        "trailing_4d": df.iloc[max(0, len(df) - 4 * bars_per_day):].copy(),
    }
    return {name: summarize_window(window, bars_per_year) for name, window in windows.items() if len(window) > 1}


def benchmark_returns(histories: dict[str, pd.DataFrame]) -> pd.Series:
    for pair in PAIRS:
        if pair not in histories:
            raise ValueError(f"Missing history for {pair}")
        if histories[pair].empty:
            raise ValueError(f"Empty history for {pair} in benchmark_returns")
        if "close" not in histories[pair].columns:
            raise ValueError(f"Missing close column for {pair}")

    base = histories[PAIRS[0]][["timestamp"]].copy().reset_index(drop=True)
    if base.empty:
        raise ValueError("Base history is empty in benchmark_returns")

    bench = pd.Series(0.0, index=base.index, dtype=float)
    for pair in PAIRS:
        close = histories[pair]["close"].astype(float).reset_index(drop=True)
        if close.empty:
            raise ValueError(f"{pair} close series is empty in benchmark_returns")
        first_close = float(close.iloc[0])
        if first_close <= 0:
            raise ValueError(f"{pair} first close must be positive, got {first_close}")
        bench += TARGET_WEIGHTS[pair] * (close / first_close)

    out = bench.pct_change().fillna(0.0)
    out.index = base["timestamp"]
    return out


def compute_regime_metrics(merged: pd.DataFrame, benchmark_rets: pd.Series, window_name: str) -> dict[str, dict]:
    bot_returns = merged["equity"].pct_change().fillna(0.0)
    bot_returns.index = merged["timestamp"]
    bench = benchmark_rets.reindex(bot_returns.index).fillna(0.0)

    regimes = {
        "all": np.full(len(bot_returns), True),
        "up_market": (bench > 0).to_numpy(),
        "down_market": (bench < 0).to_numpy(),
    }

    out: dict[str, dict] = {}
    for regime, mask in regimes.items():
        series = bot_returns[mask]
        bench_series = bench[mask]
        if len(series) == 0:
            out[regime] = {
                f"{window_name}_return": 0.0,
                f"{window_name}_benchmark_return": 0.0,
                f"{window_name}_bar_count": 0,
            }
            continue

        bot_cum = float(np.prod(1.0 + series.to_numpy()) - 1.0)
        bench_cum = float(np.prod(1.0 + bench_series.to_numpy()) - 1.0)
        avg_bot = float(series.mean())
        avg_bench = float(bench_series.mean())
        capture = 0.0
        if regime == "up_market" and avg_bench > 0:
            capture = avg_bot / avg_bench
        elif regime == "down_market" and avg_bench < 0:
            capture = avg_bot / avg_bench

        out[regime] = {
            f"{window_name}_return": bot_cum,
            f"{window_name}_benchmark_return": bench_cum,
            f"{window_name}_bar_count": int(len(series)),
            f"{window_name}_avg_bot_bar_return": avg_bot,
            f"{window_name}_avg_benchmark_bar_return": avg_bench,
            f"{window_name}_capture_ratio": float(capture),
        }
    return out


def make_config(pair: str, params: SweepParams) -> GridConfig:
    weight = TARGET_WEIGHTS[pair]
    target_notional = 1_000_000.0 * weight
    return GridConfig(
        pair=pair,
        levels_per_side=params.levels_per_side,
        spacing_pct=params.spacing_pct,
        buy_spacing_multiplier=params.buy_spacing_multiplier,
        sell_spacing_multiplier=params.sell_spacing_multiplier,
        base_per_level_notional_usd=target_notional * params.order_size_pct_of_target,
        base_max_position_notional_usd=target_notional,
        per_level_notional_usd=target_notional * params.order_size_pct_of_target,
        max_position_notional_usd=target_notional,
        target_weight_pct=weight,
        order_size_pct_of_target=params.order_size_pct_of_target,
        max_position_pct_of_target=1.0,
        min_inventory_floor_pct_of_target=params.min_inventory_floor_pct_of_target,
        adaptive_order_levels=params.adaptive_order_levels,
        refresh_threshold_pct=0.05,
        cash_reserve_pct=params.cash_reserve_pct,
        max_open_orders=64,
        max_24h_abs_change_pct=0.12,
        reanchor_after_fill=True,
        pause_guard=True,
        enable_signal_tilt=True,
        signal_spacing_tilt_pct=params.signal_spacing_tilt_pct,
        signal_extra_levels_per_score=params.signal_extra_levels_per_score,
    )


def release_fraction_for_conditions(
    *,
    pair_drop_pct: float,
    portfolio_drawdown_pct: float,
    params: SweepParams,
) -> float:
    pair_severity = 0.0
    if pair_drop_pct <= -params.crash_trigger_pct:
        pair_severity = abs(pair_drop_pct) / max(params.crash_trigger_pct, 1e-9) - 1.0

    dd_severity = 0.0
    if portfolio_drawdown_pct <= -params.portfolio_drawdown_trigger_pct:
        dd_severity = abs(portfolio_drawdown_pct) / max(params.portfolio_drawdown_trigger_pct, 1e-9) - 1.0

    severity = max(pair_severity, dd_severity, 0.0)
    if severity <= 0.0:
        return 0.0

    frac = params.emergency_release_frac + params.emergency_release_slope * severity
    return float(max(0.0, min(1.0, frac)))


def build_dynamic_runtime(
    *,
    config: GridConfig,
    params: SweepParams,
    cash: float,
    qty_by_pair: dict[str, float],
    open_prices: dict[str, float],
    pair: str,
    pair_change_24h: float,
    portfolio_drawdown_pct: float,
) -> DynamicRuntime:
    total_equity = cash + sum(qty_by_pair[p] * open_prices[p] for p in PAIRS)
    target_notional = total_equity * config.target_weight_pct
    current_notional = qty_by_pair[pair] * open_prices[pair]

    base_reserve = total_equity * params.cash_reserve_pct
    emergency_reserve = total_equity * params.emergency_reserve_pct
    release_fraction = release_fraction_for_conditions(
        pair_drop_pct=pair_change_24h,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
        params=params,
    )
    locked_emergency = emergency_reserve * (1.0 - release_fraction)

    investable_cap = target_notional * config.max_position_pct_of_target
    buy_gap = max(investable_cap - current_notional, 0.0)
    inventory_floor = max(target_notional * config.min_inventory_floor_pct_of_target, 0.0)
    sell_gap = max(current_notional - inventory_floor, 0.0)

    preferred_order = target_notional * config.order_size_pct_of_target
    levels = max(config.adaptive_order_levels, 1)

    buy_room_ratio = buy_gap / investable_cap if investable_cap > 0 else 0.0
    buy_scale = params.buy_room_min_mult + (params.buy_room_max_mult - params.buy_room_min_mult) * (buy_room_ratio ** params.inventory_room_power)
    buy_scale = float(max(params.buy_room_min_mult, min(params.buy_room_max_mult, buy_scale)))

    sell_room_denom = max(investable_cap - inventory_floor, 1e-9)
    sell_room_ratio = sell_gap / sell_room_denom if sell_room_denom > 0 else 0.0
    sell_scale = params.sell_room_min_mult + (params.sell_room_max_mult - params.sell_room_min_mult) * (sell_room_ratio ** params.inventory_room_power)
    sell_scale = float(max(params.sell_room_min_mult, min(params.sell_room_max_mult, sell_scale)))

    wallet_deployable_cash = max(cash - base_reserve - locked_emergency, 0.0)

    buy_notional = min(preferred_order * buy_scale, buy_gap / levels) if buy_gap > 0 else 0.0
    sell_notional = min(preferred_order * sell_scale, sell_gap / levels) if sell_gap > 0 else 0.0

    return DynamicRuntime(
        current_qty=qty_by_pair[pair],
        deployable_cash=wallet_deployable_cash,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        max_position=investable_cap,
        emergency_release_fraction=release_fraction,
        emergency_locked_cash=locked_emergency,
        base_reserve_cash=base_reserve,
        current_notional=current_notional,
        target_notional=target_notional,
        buy_gap=buy_gap,
        sell_gap=sell_gap,
    )


def simulate_dynamic_portfolio(
    histories: dict[str, pd.DataFrame],
    *,
    params: SweepParams,
    interval: str,
    initial_equity: float = 1_000_000.0,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    configs = {pair: make_config(pair, params) for pair in PAIRS}
    rules = {pair: PairRules(pair=pair, price_precision=2, amount_precision=6, min_order_value=1.0) for pair in PAIRS}
    strategies = {pair: GridStrategy(configs[pair], rules[pair]) for pair in PAIRS}
    changes = {pair: compute_change_24h(histories[pair]) for pair in PAIRS}

    initial_base_reserve = initial_equity * params.cash_reserve_pct
    initial_emergency_reserve = initial_equity * params.emergency_reserve_pct
    investable = initial_equity - initial_base_reserve - initial_emergency_reserve
    cash = initial_base_reserve + initial_emergency_reserve
    qty_by_pair = {
        pair: investable * TARGET_WEIGHTS[pair] / float(histories[pair].iloc[0]["open"])
        for pair in PAIRS
    }
    avg_cost = {pair: float(histories[pair].iloc[0]["open"]) for pair in PAIRS}

    running_peak = initial_equity
    trades: list[dict] = []
    equity_rows: list[dict] = []
    weight_rows: list[dict] = []
    bars_per_day = bars_per_day_from_interval(interval)
    day_fraction = 1.0 / bars_per_day

    for i in range(len(histories[PAIRS[0]])):
        timestamp = histories[PAIRS[0]].iloc[i]["timestamp"]
        opens = {pair: float(histories[pair].iloc[i]["open"]) for pair in PAIRS}
        highs = {pair: float(histories[pair].iloc[i]["high"]) for pair in PAIRS}
        lows = {pair: float(histories[pair].iloc[i]["low"]) for pair in PAIRS}
        closes = {pair: float(histories[pair].iloc[i]["close"]) for pair in PAIRS}

        pre_bar_equity = cash + sum(qty_by_pair[pair] * opens[pair] for pair in PAIRS)
        running_peak = max(running_peak, pre_bar_equity)
        portfolio_drawdown_pct = pre_bar_equity / running_peak - 1.0 if running_peak > 0 else 0.0

        bar_trade_count = 0
        bar_buys = 0
        bar_sells = 0
        release_fracs = []
        locked_emergency_cash = []
        base_reserve_cash = []

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

            runtime = build_dynamic_runtime(
                config=configs[pair],
                params=params,
                cash=cash,
                qty_by_pair=qty_by_pair,
                open_prices=opens,
                pair=pair,
                pair_change_24h=float(changes[pair][i]),
                portfolio_drawdown_pct=portfolio_drawdown_pct,
            )
            release_fracs.append(runtime.emergency_release_fraction)
            locked_emergency_cash.append(runtime.emergency_locked_cash)
            base_reserve_cash.append(runtime.base_reserve_cash)

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
                    avg_cost[pair] = total_cost / qty_by_pair[pair] if qty_by_pair[pair] > 0 else 0.0
                    cash -= notional + fee
                    bar_trade_count += 1
                    bar_buys += 1
                    trades.append(
                        {
                            "timestamp": timestamp,
                            "pair": pair,
                            "side": "BUY",
                            "price": order.price,
                            "quantity": order.quantity,
                            "fee": fee,
                            "release_fraction": runtime.emergency_release_fraction,
                            "locked_emergency_cash": runtime.emergency_locked_cash,
                            "portfolio_drawdown_pct": portfolio_drawdown_pct,
                            "pair_change_24h": float(changes[pair][i]),
                        }
                    )
                    if configs[pair].reanchor_after_fill:
                        strategies[pair].set_anchor(order.price)
                elif order.side == "SELL" and qty_by_pair[pair] >= order.quantity:
                    qty_by_pair[pair] -= order.quantity
                    cash += notional - fee
                    bar_trade_count += 1
                    bar_sells += 1
                    trades.append(
                        {
                            "timestamp": timestamp,
                            "pair": pair,
                            "side": "SELL",
                            "price": order.price,
                            "quantity": order.quantity,
                            "fee": fee,
                            "release_fraction": runtime.emergency_release_fraction,
                            "locked_emergency_cash": runtime.emergency_locked_cash,
                            "portfolio_drawdown_pct": portfolio_drawdown_pct,
                            "pair_change_24h": float(changes[pair][i]),
                        }
                    )
                    if qty_by_pair[pair] <= 0:
                        qty_by_pair[pair] = 0.0
                        avg_cost[pair] = 0.0
                    if configs[pair].reanchor_after_fill:
                        strategies[pair].set_anchor(order.price)

        equity = cash + sum(qty_by_pair[pair] * closes[pair] for pair in PAIRS)
        benchmark_equity = initial_equity * sum(
            TARGET_WEIGHTS[pair] * (closes[pair] / float(histories[pair].iloc[0]["close"]))
            for pair in PAIRS
        )
        weights = {pair: (qty_by_pair[pair] * closes[pair] / equity if equity > 0 else 0.0) for pair in PAIRS}
        cash_weight = cash / equity if equity > 0 else 0.0
        drift = sum(abs(weights[pair] - TARGET_WEIGHTS[pair]) for pair in PAIRS)

        equity_rows.append(
            {
                "timestamp": timestamp,
                "equity": equity,
                "cash": cash,
                "benchmark_equity": benchmark_equity,
                "bar_trade_count": bar_trade_count,
                "bar_buys": bar_buys,
                "bar_sells": bar_sells,
                "release_fraction": float(max(release_fracs) if release_fracs else 0.0),
                "locked_emergency_cash": float(max(locked_emergency_cash) if locked_emergency_cash else 0.0),
                "base_reserve_cash": float(max(base_reserve_cash) if base_reserve_cash else 0.0),
                "bar_day_fraction": day_fraction,
            }
        )
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


def score_trial(summaries: dict[str, dict], regime_metrics: dict[str, dict], full_label: str) -> float:
    full_m = summaries[full_label]
    t21 = summaries.get("trailing_21d", full_m)
    t10 = summaries.get("trailing_10d", full_m)
    t4 = summaries.get("trailing_4d", t10)

    full_down_cap = regime_metrics.get(full_label, {}).get("down_market", {}).get(f"{full_label}_capture_ratio", 0.0)
    full_up_cap = regime_metrics.get(full_label, {}).get("up_market", {}).get(f"{full_label}_capture_ratio", 0.0)

    score = 0.0
    score += 2.2 * t4["total_return"]
    score += 1.5 * t10["total_return"]
    score += 0.9 * t21["total_return"]
    score += 0.35 * full_m["total_return"]
    score += 0.12 * t10["sharpe_like"]
    score += 0.10 * t10["sortino_like"]
    score += 0.08 * t10["calmar_like"]
    score += 0.10 * full_up_cap
    score -= 0.30 * abs(t4["max_drawdown"])
    score -= 0.24 * abs(t10["max_drawdown"])
    score -= 0.12 * abs(t21["max_drawdown"])
    score -= 0.08 * abs(full_m["max_drawdown"])
    score -= 0.06 * t10["avg_cash_weight"]
    score -= 0.05 * t10["avg_abs_weight_drift"]
    score -= 0.10 * max(full_down_cap - 1.0, 0.0)
    return float(score)


def _compute_regimes_for_summaries(
    merged: pd.DataFrame,
    benchmark_rets: pd.Series,
    interval: str,
    full_label: str,
    summaries: dict[str, dict],
) -> dict[str, dict]:
    regimes: dict[str, dict] = {}
    bars_per_day = bars_per_day_from_interval(interval)
    for window in summaries:
        if window == full_label:
            window_df = merged
        elif window == "trailing_21d":
            window_df = merged.iloc[max(0, len(merged) - 21 * bars_per_day):].copy()
        elif window == "trailing_10d":
            window_df = merged.iloc[max(0, len(merged) - 10 * bars_per_day):].copy()
        elif window == "trailing_4d":
            window_df = merged.iloc[max(0, len(merged) - 4 * bars_per_day):].copy()
        else:
            window_df = merged
        regimes[window] = compute_regime_metrics(window_df, benchmark_rets, window)
    return regimes


def _evaluate_candidate_from_context(
    idx: int,
    params: SweepParams,
    histories: dict[str, pd.DataFrame],
    benchmark_rets: pd.Series,
    interval: str,
    initial_equity: float,
    full_label: str,
) -> dict:
    merged, _, _ = simulate_dynamic_portfolio(
        histories,
        params=params,
        interval=interval,
        initial_equity=initial_equity,
    )
    summaries = build_window_summaries(merged, interval, full_label)
    regimes = _compute_regimes_for_summaries(merged, benchmark_rets, interval, full_label, summaries)
    aggregate_score = score_trial(summaries, regimes, full_label)

    row = {
        "trial": idx,
        **params.to_flat_dict(),
        "aggregate_score": aggregate_score,
        "full_return": summaries[full_label]["total_return"],
        "full_drawdown": summaries[full_label]["max_drawdown"],
        "full_sharpe": summaries[full_label]["sharpe_like"],
        "full_sortino": summaries[full_label]["sortino_like"],
        "full_calmar": summaries[full_label]["calmar_like"],
        "full_benchmark_return": summaries[full_label]["benchmark_return"],
        "trailing_21d_return": summaries.get("trailing_21d", summaries[full_label])["total_return"],
        "trailing_21d_drawdown": summaries.get("trailing_21d", summaries[full_label])["max_drawdown"],
        "trailing_10d_return": summaries.get("trailing_10d", summaries[full_label])["total_return"],
        "trailing_10d_drawdown": summaries.get("trailing_10d", summaries[full_label])["max_drawdown"],
        "trailing_4d_return": summaries.get("trailing_4d", summaries[full_label])["total_return"],
        "trailing_4d_drawdown": summaries.get("trailing_4d", summaries[full_label])["max_drawdown"],
        "avg_cash_weight": summaries[full_label]["avg_cash_weight"],
        "avg_abs_weight_drift": summaries[full_label]["avg_abs_weight_drift"],
        "avg_release_fraction": summaries[full_label]["avg_release_fraction"],
        "avg_locked_emergency_cash": summaries[full_label]["avg_locked_emergency_cash"],
        "full_up_capture": regimes[full_label]["up_market"].get(f"{full_label}_capture_ratio", 0.0),
        "full_down_capture": regimes[full_label]["down_market"].get(f"{full_label}_capture_ratio", 0.0),
        "trade_count": summaries[full_label]["trade_count"],
        "trades_per_day": summaries[full_label]["trades_per_day"],
    }

    return {
        "idx": idx,
        "params": params,
        "aggregate_score": aggregate_score,
        "row": row,
    }


def _init_worker(
    histories: dict[str, pd.DataFrame],
    benchmark_rets: pd.Series,
    interval: str,
    initial_equity: float,
    full_label: str,
) -> None:
    global _WORKER_HISTORIES, _WORKER_BENCH_RETS, _WORKER_INTERVAL, _WORKER_INITIAL_EQUITY, _WORKER_FULL_LABEL
    _WORKER_HISTORIES = histories
    _WORKER_BENCH_RETS = benchmark_rets
    _WORKER_INTERVAL = interval
    _WORKER_INITIAL_EQUITY = initial_equity
    _WORKER_FULL_LABEL = full_label


def _evaluate_candidate_worker(task: tuple[int, SweepParams]) -> dict:
    idx, params = task
    if (
        _WORKER_HISTORIES is None
        or _WORKER_BENCH_RETS is None
        or _WORKER_INTERVAL is None
        or _WORKER_INITIAL_EQUITY is None
        or _WORKER_FULL_LABEL is None
    ):
        raise RuntimeError("Worker context is not initialized")

    return _evaluate_candidate_from_context(
        idx,
        params,
        _WORKER_HISTORIES,
        _WORKER_BENCH_RETS,
        _WORKER_INTERVAL,
        _WORKER_INITIAL_EQUITY,
        _WORKER_FULL_LABEL,
    )


def _axes_from_args(args: argparse.Namespace) -> list[list[float | int]]:
    return [
        parse_float_list(args.order_size_pcts),
        parse_float_list(args.cash_reserves),
        parse_float_list(args.emergency_reserves),
        parse_float_list(args.crash_triggers),
        parse_float_list(args.portfolio_drawdown_triggers),
        parse_float_list(args.emergency_release_fracs),
        parse_float_list(args.emergency_release_slopes),
        parse_int_list(args.adaptive_levels),
        parse_int_list(args.levels_per_side),
        parse_float_list(args.spacing_pcts),
        parse_float_list(args.buy_spacing_mults),
        parse_float_list(args.sell_spacing_mults),
        parse_float_list(args.min_inventory_floor_pcts),
        parse_float_list(args.inventory_room_powers),
        parse_float_list(args.buy_room_min_mults),
        parse_float_list(args.buy_room_max_mults),
        parse_float_list(args.sell_room_min_mults),
        parse_float_list(args.sell_room_max_mults),
        parse_float_list(args.signal_spacing_tilts),
        parse_int_list(args.signal_extra_levels),
    ]


def _combo_to_params(combo: tuple[float | int, ...]) -> SweepParams:
    os, cr, er, ct, pdt, rf, rs, al, lps, sp, bsm, ssm, mif, irp, brmin, brmax, srmin, srmax, sst, sel = combo
    return SweepParams(
        order_size_pct_of_target=float(os),
        cash_reserve_pct=float(cr),
        emergency_reserve_pct=float(er),
        crash_trigger_pct=float(ct),
        portfolio_drawdown_trigger_pct=float(pdt),
        emergency_release_frac=float(rf),
        emergency_release_slope=float(rs),
        adaptive_order_levels=int(al),
        levels_per_side=int(lps),
        spacing_pct=float(sp),
        buy_spacing_multiplier=float(bsm),
        sell_spacing_multiplier=float(ssm),
        min_inventory_floor_pct_of_target=float(mif),
        inventory_room_power=float(irp),
        buy_room_min_mult=float(brmin),
        buy_room_max_mult=float(brmax),
        sell_room_min_mult=float(srmin),
        sell_room_max_mult=float(srmax),
        signal_spacing_tilt_pct=float(sst),
        signal_extra_levels_per_score=int(sel),
    )


def _is_valid_params(p: SweepParams) -> bool:
    if p.cash_reserve_pct < 0 or p.emergency_reserve_pct < 0:
        return False
    if p.cash_reserve_pct + p.emergency_reserve_pct >= 0.80:
        return False
    if p.order_size_pct_of_target <= 0:
        return False
    if p.buy_room_max_mult < p.buy_room_min_mult or p.sell_room_max_mult < p.sell_room_min_mult:
        return False
    if p.crash_trigger_pct <= 0 or p.portfolio_drawdown_trigger_pct <= 0:
        return False
    if p.adaptive_order_levels <= 0 or p.levels_per_side <= 0:
        return False
    return True


def trial_param_grid(args: argparse.Namespace) -> tuple[list[SweepParams], int]:
    axes = _axes_from_args(args)
    total_combinations = math.prod(len(axis) for axis in axes)
    max_trials = int(args.max_trials)
    seed = int(args.seed)

    if max_trials > 0 and total_combinations > max_trials:
        rng = random.Random(seed)
        seen: set[tuple[int, ...]] = set()
        params: list[SweepParams] = []
        target = max_trials
        while len(params) < target:
            idxs = tuple(rng.randrange(len(axis)) for axis in axes)
            if idxs in seen:
                continue
            seen.add(idxs)
            combo = tuple(axis[i] for axis, i in zip(axes, idxs))
            p = _combo_to_params(combo)
            if _is_valid_params(p):
                params.append(p)
        return params, total_combinations

    valid: list[SweepParams] = []
    for combo in itertools.product(*axes):
        p = _combo_to_params(combo)
        if _is_valid_params(p):
            valid.append(p)
    return valid, total_combinations


def write_trial_outputs(outdir: Path, label: str, artifacts: TrialArtifacts, full_label: str) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    artifacts.merged.to_csv(outdir / f"{label}_equity_curve.csv", index=False)
    artifacts.weights.to_csv(outdir / f"{label}_weights.csv", index=False)
    pd.DataFrame(artifacts.trades).to_csv(outdir / f"{label}_trades.csv", index=False)
    with open(outdir / f"{label}_params.json", "w", encoding="utf-8") as f:
        json.dump(artifacts.params.to_flat_dict(), f, indent=2)
    with open(outdir / f"{label}_summaries.json", "w", encoding="utf-8") as f:
        json.dump(artifacts.summaries, f, indent=2)
    with open(outdir / f"{label}_regimes.json", "w", encoding="utf-8") as f:
        json.dump(artifacts.regime_metrics, f, indent=2)
    (outdir / f"{label}_report.md").write_text(render_artifact_report(artifacts, full_label), encoding="utf-8")


def render_artifact_report(artifacts: TrialArtifacts, full_label: str) -> str:
    lines = ["# Heavy Dynamic Sweep Winner", "", "## Params"]
    for k, v in artifacts.params.to_flat_dict().items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", f"- aggregate_score: {artifacts.aggregate_score}", ""])
    for window in [full_label, "trailing_21d", "trailing_10d", "trailing_4d"]:
        if window not in artifacts.summaries:
            continue
        lines.append(f"## {window}")
        for k, v in artifacts.summaries[window].items():
            lines.append(f"- {k}: {v}")
        if window in artifacts.regime_metrics:
            lines.append("")
            lines.append(f"### {window} regimes")
            for regime, metrics in artifacts.regime_metrics[window].items():
                lines.append(f"#### {regime}")
                for k, v in metrics.items():
                    lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Focused parameter sweep around Trial 620 for the dynamic 3-bot portfolio.")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--eth-csv", type=str, default=None)
    parser.add_argument("--sol-csv", type=str, default=None)
    parser.add_argument("--btc-csv", type=str, default=None)
    parser.add_argument("--outdir", type=str, default="artifacts_heavy_dynamic_sweep")
    parser.add_argument("--initial-equity", type=float, default=1_000_000.0)
    parser.add_argument("--max-trials", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--workers", type=int, default=16)

    parser.add_argument("--order-size-pcts", type=str, default="0.022")
    parser.add_argument("--cash-reserves", type=str, default="0.10,0.12,0.14")
    parser.add_argument("--emergency-reserves", type=str, default="0.05,0.08")
    parser.add_argument("--crash-triggers", type=str, default="0.08,0.10")
    parser.add_argument("--portfolio-drawdown-triggers", type=str, default="0.08,0.10")
    parser.add_argument("--emergency-release-fracs", type=str, default="0.60")
    parser.add_argument("--emergency-release-slopes", type=str, default="1.25")
    parser.add_argument("--adaptive-levels", type=str, default="4,6,8")
    parser.add_argument("--levels-per-side", type=str, default="18,20,25")
    parser.add_argument("--spacing-pcts", type=str, default="0.011")
    parser.add_argument("--buy-spacing-mults", type=str, default="1.00,1.05,1.10")
    parser.add_argument("--sell-spacing-mults", type=str, default="1.00")
    parser.add_argument("--min-inventory-floor-pcts", type=str, default="0.08")
    parser.add_argument("--inventory-room-powers", type=str, default="1.0,1.5")
    parser.add_argument("--buy-room-min-mults", type=str, default="0.60,0.70")
    parser.add_argument("--buy-room-max-mults", type=str, default="1.00")
    parser.add_argument("--sell-room-min-mults", type=str, default="0.75")
    parser.add_argument("--sell-room-max-mults", type=str, default="1.15")
    parser.add_argument("--signal-spacing-tilts", type=str, default="0.05")
    parser.add_argument("--signal-extra-levels", type=str, default="2")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    histories = load_histories(
        days=args.days,
        interval=args.interval,
        csv_by_pair={"ETH/USD": args.eth_csv, "SOL/USD": args.sol_csv, "BTC/USD": args.btc_csv},
    )
    bench_rets = benchmark_returns(histories)
    full_label = f"full_{args.days}d"

    baseline = SweepParams(
        order_size_pct_of_target=0.022,
        cash_reserve_pct=0.07,
        emergency_reserve_pct=0.00,
        crash_trigger_pct=0.10,
        portfolio_drawdown_trigger_pct=0.08,
        emergency_release_frac=0.0,
        emergency_release_slope=0.0,
        adaptive_order_levels=10,
        levels_per_side=25,
        spacing_pct=0.011,
        buy_spacing_multiplier=1.0,
        sell_spacing_multiplier=1.0,
        min_inventory_floor_pct_of_target=0.10,
        inventory_room_power=1.0,
        buy_room_min_mult=1.0,
        buy_room_max_mult=1.0,
        sell_room_min_mult=1.0,
        sell_room_max_mult=1.0,
        signal_spacing_tilt_pct=0.05,
        signal_extra_levels_per_score=2,
    )

    trial_620 = SweepParams(
        order_size_pct_of_target=0.022,
        cash_reserve_pct=0.12,
        emergency_reserve_pct=0.08,
        crash_trigger_pct=0.08,
        portfolio_drawdown_trigger_pct=0.10,
        emergency_release_frac=0.60,
        emergency_release_slope=1.25,
        adaptive_order_levels=6,
        levels_per_side=20,
        spacing_pct=0.011,
        buy_spacing_multiplier=1.05,
        sell_spacing_multiplier=1.00,
        min_inventory_floor_pct_of_target=0.08,
        inventory_room_power=1.0,
        buy_room_min_mult=0.70,
        buy_room_max_mult=1.00,
        sell_room_min_mult=0.75,
        sell_room_max_mult=1.15,
        signal_spacing_tilt_pct=0.05,
        signal_extra_levels_per_score=2,
    )

    sampled_params, total_combinations = trial_param_grid(args)
    candidates = [baseline, trial_620] + sampled_params
    deduped: list[SweepParams] = []
    seen_params: set[SweepParams] = set()
    for c in candidates:
        if c not in seen_params:
            deduped.append(c)
            seen_params.add(c)
    candidates = deduped

    trial_rows: list[dict] = []
    total_candidates = len(candidates)
    progress_every = max(int(args.progress_every), 1)
    workers = max(int(args.workers), 1)
    started_at = time.perf_counter()
    results: list[dict] = []
    best_score_seen = float("-inf")

    if workers == 1:
        for idx, params in enumerate(candidates, start=1):
            result = _evaluate_candidate_from_context(
                idx,
                params,
                histories,
                bench_rets,
                args.interval,
                args.initial_equity,
                full_label,
            )
            results.append(result)
            if result["aggregate_score"] > best_score_seen:
                best_score_seen = result["aggregate_score"]

            if idx == 1 or idx % progress_every == 0 or idx == total_candidates:
                elapsed = time.perf_counter() - started_at
                avg_per_trial = elapsed / idx if idx > 0 else 0.0
                remaining = max(total_candidates - idx, 0)
                eta = avg_per_trial * remaining
                print(
                    f"Progress {idx}/{total_candidates} | elapsed={elapsed:.1f}s | "
                    f"eta={eta:.1f}s | best_score={best_score_seen:.6f}",
                    flush=True,
                )
    else:
        with cf.ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(histories, bench_rets, args.interval, args.initial_equity, full_label),
        ) as executor:
            futures = [
                executor.submit(_evaluate_candidate_worker, (idx, params))
                for idx, params in enumerate(candidates, start=1)
            ]

            completed = 0
            for future in cf.as_completed(futures):
                result = future.result()
                results.append(result)
                completed += 1
                if result["aggregate_score"] > best_score_seen:
                    best_score_seen = result["aggregate_score"]

                if completed == 1 or completed % progress_every == 0 or completed == total_candidates:
                    elapsed = time.perf_counter() - started_at
                    avg_per_trial = elapsed / completed if completed > 0 else 0.0
                    remaining = max(total_candidates - completed, 0)
                    eta = avg_per_trial * remaining
                    print(
                        f"Progress {completed}/{total_candidates} | elapsed={elapsed:.1f}s | "
                        f"eta={eta:.1f}s | best_score={best_score_seen:.6f}",
                        flush=True,
                    )

    results = sorted(results, key=lambda r: int(r["idx"]))
    trial_rows = [r["row"] for r in results]
    best_result = max(results, key=lambda r: float(r["aggregate_score"]))
    best_params: SweepParams = best_result["params"]

    best_merged, best_weights, best_trades = simulate_dynamic_portfolio(
        histories,
        params=best_params,
        interval=args.interval,
        initial_equity=args.initial_equity,
    )
    best_summaries = build_window_summaries(best_merged, args.interval, full_label)
    best_regimes = _compute_regimes_for_summaries(best_merged, bench_rets, args.interval, full_label, best_summaries)
    best = TrialArtifacts(
        params=best_params,
        merged=best_merged,
        weights=best_weights,
        trades=best_trades,
        summaries=best_summaries,
        regime_metrics=best_regimes,
        aggregate_score=float(best_result["aggregate_score"]),
    )

    all_trials = pd.DataFrame(trial_rows).sort_values("aggregate_score", ascending=False).reset_index(drop=True)
    top_trials = all_trials.head(args.top_k).copy()
    all_trials.to_csv(outdir / "all_trials.csv", index=False)
    top_trials.to_csv(outdir / "top_trials.csv", index=False)

    if best is None:
        raise RuntimeError("No trials completed")

    best_payload = {
        "aggregate_score": best.aggregate_score,
        "params": best.params.to_flat_dict(),
        "summaries": best.summaries,
        "regime_metrics": best.regime_metrics,
    }
    with open(outdir / "best_trial.json", "w", encoding="utf-8") as f:
        json.dump(best_payload, f, indent=2)

    write_trial_outputs(outdir / "best_trial_artifacts", "best_trial", best, full_label)

    lines = [
        "# Heavy Dynamic Sweep",
        "",
        f"- days: {args.days}",
        f"- interval: {args.interval}",
        f"- workers: {workers}",
        f"- total_trials_run: {len(all_trials)}",
        f"- full_grid_combinations: {total_combinations}",
        f"- sampled_max_trials: {args.max_trials}",
        "",
        "## Best Params",
    ]
    for k, v in best.params.to_flat_dict().items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", f"- aggregate_score: {best.aggregate_score}", ""])

    for window in [full_label, "trailing_21d", "trailing_10d", "trailing_4d"]:
        if window not in best.summaries:
            continue
        lines.append(f"## {window}")
        for k, v in best.summaries[window].items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        if window in best.regime_metrics:
            lines.append(f"### {window} regimes")
            for regime, metrics in best.regime_metrics[window].items():
                lines.append(f"#### {regime}")
                for k, v in metrics.items():
                    lines.append(f"- {k}: {v}")
            lines.append("")

    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
