from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import pandas as pd

from config import GridConfig, PairRules

log = logging.getLogger("strategy")


@dataclass(slots=True)
class DesiredOrder:
    side: str
    price: float
    quantity: float

    def __repr__(self) -> str:
        return f"{self.side} {self.quantity:.8f} @ {self.price:.2f} (notional={self.price * self.quantity:.2f})"


@dataclass(slots=True)
class TickerView:
    bid: float
    ask: float
    last: float
    change_24h: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def __repr__(self) -> str:
        return (
            f"TickerView(bid={self.bid:.2f}, ask={self.ask:.2f}, "
            f"last={self.last:.2f}, mid={self.mid:.2f}, change_24h={self.change_24h:.4%})"
        )


class GridStrategy:
    def __init__(self, config: GridConfig, rules: PairRules) -> None:
        self.config = config
        self.rules = rules
        self.anchor_price: float | None = None
        self.price_history: deque[float] = deque(maxlen=config.signal_history_limit)
        log.debug("GridStrategy created | rules=%s", rules)

    def record_price(self, price: float) -> None:
        self.price_history.append(float(price))

    def should_pause(self, ticker: TickerView) -> bool:
        return abs(ticker.change_24h) >= self.config.max_24h_abs_change_pct

    def should_refresh(self, ticker: TickerView) -> bool:
        if self.anchor_price is None:
            return True
        drift = abs(ticker.mid - self.anchor_price) / self.anchor_price
        return drift >= self.config.refresh_threshold_pct

    def set_anchor(self, price: float) -> None:
        self.anchor_price = price

    def _signal_tilt(self) -> int:
        if not self.config.enable_signal_tilt:
            return 0

        if len(self.price_history) < max(self.config.signal_slow_window, self.config.signal_bb_window):
            return 0

        series = pd.Series(self.price_history, dtype="float64")
        fast_ma = series.rolling(self.config.signal_fast_window).mean().iloc[-1]
        slow_ma = series.rolling(self.config.signal_slow_window).mean().iloc[-1]

        bb_mid = series.rolling(self.config.signal_bb_window).mean().iloc[-1]
        bb_std = series.rolling(self.config.signal_bb_window).std(ddof=0).iloc[-1]
        bb_upper = bb_mid + self.config.signal_bb_std_mult * bb_std
        bb_lower = bb_mid - self.config.signal_bb_std_mult * bb_std
        last_price = series.iloc[-1]

        score = 0

        if fast_ma > slow_ma:
            score += 1
        elif fast_ma < slow_ma:
            score -= 1

        tolerance = self.config.signal_band_touch_tolerance_pct
        if last_price <= bb_lower * (1 + tolerance):
            score += 1
        elif last_price >= bb_upper * (1 - tolerance):
            score -= 1

        return score

    def desired_orders(
        self,
        ticker: TickerView,
        coin_position: float,
        usd_free: float | None = None,
    ) -> list[DesiredOrder]:
        if self.anchor_price is None:
            self.anchor_price = ticker.mid

        if usd_free is None:
            usd_free = float("inf")

        anchor = self.anchor_price
        score = self._signal_tilt()

        buy_spacing_mult = self.config.buy_spacing_multiplier
        sell_spacing_mult = self.config.sell_spacing_multiplier
        buy_extra_levels = 0
        sell_extra_levels = 0

        if score > 0:
            buy_spacing_mult *= 1.0 - self.config.signal_spacing_tilt_pct
            sell_spacing_mult *= 1.0 + self.config.signal_spacing_tilt_pct
            buy_extra_levels = score * self.config.signal_extra_levels_per_score
        elif score < 0:
            buy_spacing_mult *= 1.0 + self.config.signal_spacing_tilt_pct
            sell_spacing_mult *= 1.0 - self.config.signal_spacing_tilt_pct
            sell_extra_levels = abs(score) * self.config.signal_extra_levels_per_score

        desired: list[DesiredOrder] = []

        max_buy_levels = self.config.levels_per_side + buy_extra_levels
        max_sell_levels = self.config.levels_per_side + sell_extra_levels

        for level in range(1, max_buy_levels + 1):
            level_offset = self.config.nearest_buy_offset_multiplier + (level - 1)
            raw_price = anchor * (1 - self.config.spacing_pct * level_offset * buy_spacing_mult)
            price = self.rules.round_price(raw_price)
            if price <= 0:
                continue

            qty = self.config.order_quantity(price, self.rules)
            if qty <= 0:
                continue

            notional = price * qty
            if notional < self.rules.min_order_value:
                continue

            projected_notional = coin_position * ticker.mid + notional
            if projected_notional > self.config.max_position_notional_usd:
                continue

            if usd_free < notional:
                continue

            desired.append(DesiredOrder("BUY", price, qty))

        for level in range(1, max_sell_levels + 1):
            level_offset = self.config.nearest_sell_offset_multiplier + (level - 1)
            raw_price = anchor * (1 + self.config.spacing_pct * level_offset * sell_spacing_mult)
            price = self.rules.round_price(raw_price)
            if price <= 0:
                continue

            qty = self.config.order_quantity(price, self.rules)
            if qty <= 0:
                continue

            notional = price * qty
            if notional < self.rules.min_order_value:
                continue

            if coin_position < qty:
                continue

            desired.append(DesiredOrder("SELL", price, qty))

        return desired