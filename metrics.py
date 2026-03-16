import math

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    return float(drawdown.min())


def sharpe_ratio(returns: pd.Series, periods_per_year: int = 365) -> float:
    if returns.std(ddof=0) == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * returns.mean() / returns.std(ddof=0))


def sortino_ratio(returns: pd.Series, periods_per_year: int = 365) -> float:
    downside = returns[returns < 0]
    downside_dev = downside.std(ddof=0)
    if math.isnan(downside_dev) or downside_dev == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * returns.mean() / downside_dev)


def calmar_ratio(equity: pd.Series, periods_per_year: int = 365) -> float:
    if len(equity) < 2:
        return 0.0
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    annualized = (1 + total_return) ** (periods_per_year / max(len(equity), 1)) - 1.0
    dd = abs(max_drawdown(equity))
    if dd == 0:
        return 0.0
    return float(annualized / dd)


def summarize_equity_curve(df: pd.DataFrame) -> dict[str, float]:
    equity = df["equity"].astype(float)
    returns = equity.pct_change().fillna(0.0)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    return {
        "total_return": float(total_return),
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe_ratio(returns),
        "sortino": sortino_ratio(returns),
        "calmar": calmar_ratio(equity),
    }
