from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(slots=True)
class PairRules:
    pair: str
    price_precision: int
    amount_precision: int
    min_order_value: float

    def round_price(self, value: float) -> float:
        quant = Decimal("1").scaleb(-self.price_precision)
        return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))

    def round_amount(self, value: float) -> float:
        quant = Decimal("1").scaleb(-self.amount_precision)
        return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))


@dataclass(slots=True)
class GridConfig:
    pair: str = "ETH/USD"

    # Grid geometry
    levels_per_side: int = 20
    spacing_pct: float = 0.011
    buy_spacing_multiplier: float = 1.10
    sell_spacing_multiplier: float = 1.00
    nearest_buy_offset_multiplier: float = 1.0
    nearest_sell_offset_multiplier: float = 1.0

    # Fallback / legacy sizing
    per_level_notional_usd: float = 15000.0
    max_position_notional_usd: float = 450000.0
    base_per_level_notional_usd: float = 15000.0
    base_max_position_notional_usd: float = 450000.0

    # Portfolio-aware sizing
    target_weight_pct: float = 0.0
    order_size_pct_of_target: float = 0.022
    max_position_pct_of_target: float = 1.0
    min_inventory_floor_pct_of_target: float = 0.08
    adaptive_order_levels: int = 4

    # Core cycle / refresh
    refresh_threshold_pct: float = 0.05
    poll_seconds: int = 60
    max_open_orders: int = 64
    max_24h_abs_change_pct: float = 0.12
    reanchor_after_fill: bool = True
    pause_guard: bool = True

    # Cash / portfolio
    cash_reserve_pct: float = 0.12
    reserve_cash_usd: float = 0.0
    capital_base_usd: float = 0.0
    emergency_reserve_pct: float = 0.08
    crash_trigger_pct: float = 0.08
    portfolio_drawdown_trigger_pct: float = 0.08
    emergency_release_frac: float = 0.60
    emergency_release_slope: float = 1.25
    inventory_room_power: float = 1.0
    buy_room_min_mult: float = 0.60
    buy_room_max_mult: float = 1.00
    sell_room_min_mult: float = 0.75
    sell_room_max_mult: float = 1.15
    shared_state_file: str = "state/portfolio_state.json"

    # Refresh guards
    disable_downward_refresh_when_no_cash: bool = True
    disable_upward_refresh_when_no_coin: bool = True

    # Signal tilt
    enable_signal_tilt: bool = True
    signal_history_limit: int = 200
    signal_fast_window: int = 8
    signal_slow_window: int = 24
    signal_bb_window: int = 20
    signal_bb_std_mult: float = 2.0
    signal_band_touch_tolerance_pct: float = 0.002
    signal_spacing_tilt_pct: float = 0.05
    signal_extra_levels_per_score: int = 2

    @classmethod
    def from_env(cls) -> "GridConfig":
        base_per_level = float(os.getenv("GRID_PER_LEVEL_NOTIONAL_USD", "15000"))
        base_max_position = float(os.getenv("GRID_MAX_POSITION_NOTIONAL_USD", "450000"))
        return cls(
            pair=os.getenv("GRID_PAIR", "ETH/USD"),
            levels_per_side=int(os.getenv("GRID_LEVELS_PER_SIDE", "20")),
            spacing_pct=float(os.getenv("GRID_SPACING_PCT", "0.011")),
            buy_spacing_multiplier=float(os.getenv("GRID_BUY_SPACING_MULTIPLIER", "1.10")),
            sell_spacing_multiplier=float(os.getenv("GRID_SELL_SPACING_MULTIPLIER", "1.0")),
            nearest_buy_offset_multiplier=float(os.getenv("GRID_NEAREST_BUY_OFFSET_MULTIPLIER", "1.0")),
            nearest_sell_offset_multiplier=float(os.getenv("GRID_NEAREST_SELL_OFFSET_MULTIPLIER", "1.0")),
            per_level_notional_usd=base_per_level,
            max_position_notional_usd=base_max_position,
            base_per_level_notional_usd=base_per_level,
            base_max_position_notional_usd=base_max_position,
            target_weight_pct=float(os.getenv("GRID_TARGET_WEIGHT_PCT", "0")),
            order_size_pct_of_target=float(os.getenv("GRID_ORDER_SIZE_PCT_OF_TARGET", "0.022")),
            max_position_pct_of_target=float(os.getenv("GRID_MAX_POSITION_PCT_OF_TARGET", "1.0")),
            min_inventory_floor_pct_of_target=float(
                os.getenv("GRID_MIN_INVENTORY_FLOOR_PCT_OF_TARGET", "0.08")
            ),
            adaptive_order_levels=int(os.getenv("GRID_ADAPTIVE_ORDER_LEVELS", "4")),
            refresh_threshold_pct=float(os.getenv("GRID_REFRESH_THRESHOLD_PCT", "0.05")),
            poll_seconds=int(os.getenv("GRID_POLL_SECONDS", "60")),
            max_open_orders=int(os.getenv("GRID_MAX_OPEN_ORDERS", "64")),
            max_24h_abs_change_pct=float(os.getenv("GRID_MAX_24H_ABS_CHANGE_PCT", "0.12")),
            reanchor_after_fill=os.getenv("GRID_REANCHOR_AFTER_FILL", "true").lower() == "true",
            pause_guard=os.getenv("GRID_PAUSE_GUARD", "true").lower() == "true",
            cash_reserve_pct=float(os.getenv("GRID_CASH_RESERVE_PCT", "0.12")),
            reserve_cash_usd=float(os.getenv("GRID_RESERVE_CASH_USD", "0")),
            capital_base_usd=float(os.getenv("GRID_CAPITAL_BASE_USD", "0")),
            emergency_reserve_pct=float(os.getenv("GRID_EMERGENCY_RESERVE_PCT", "0.08")),
            crash_trigger_pct=float(os.getenv("GRID_CRASH_TRIGGER_PCT", "0.08")),
            portfolio_drawdown_trigger_pct=float(
                os.getenv("GRID_PORTFOLIO_DRAWDOWN_TRIGGER_PCT", "0.08")
            ),
            emergency_release_frac=float(os.getenv("GRID_EMERGENCY_RELEASE_FRAC", "0.60")),
            emergency_release_slope=float(os.getenv("GRID_EMERGENCY_RELEASE_SLOPE", "1.25")),
            inventory_room_power=float(os.getenv("GRID_INVENTORY_ROOM_POWER", "1.0")),
            buy_room_min_mult=float(os.getenv("GRID_BUY_ROOM_MIN_MULT", "0.60")),
            buy_room_max_mult=float(os.getenv("GRID_BUY_ROOM_MAX_MULT", "1.00")),
            sell_room_min_mult=float(os.getenv("GRID_SELL_ROOM_MIN_MULT", "0.75")),
            sell_room_max_mult=float(os.getenv("GRID_SELL_ROOM_MAX_MULT", "1.15")),
            shared_state_file=os.getenv("GRID_SHARED_STATE_FILE", "state/portfolio_state.json"),
            disable_downward_refresh_when_no_cash=(
                os.getenv("GRID_DISABLE_DOWNWARD_REFRESH_WHEN_NO_CASH", "true").lower() == "true"
            ),
            disable_upward_refresh_when_no_coin=(
                os.getenv("GRID_DISABLE_UPWARD_REFRESH_WHEN_NO_COIN", "true").lower() == "true"
            ),
            enable_signal_tilt=os.getenv("GRID_ENABLE_SIGNAL_TILT", "true").lower() == "true",
            signal_history_limit=int(os.getenv("GRID_SIGNAL_HISTORY_LIMIT", "200")),
            signal_fast_window=int(os.getenv("GRID_SIGNAL_FAST_WINDOW", "8")),
            signal_slow_window=int(os.getenv("GRID_SIGNAL_SLOW_WINDOW", "24")),
            signal_bb_window=int(os.getenv("GRID_SIGNAL_BB_WINDOW", "20")),
            signal_bb_std_mult=float(os.getenv("GRID_SIGNAL_BB_STD_MULT", "2.0")),
            signal_band_touch_tolerance_pct=float(
                os.getenv("GRID_SIGNAL_BAND_TOUCH_TOLERANCE_PCT", "0.002")
            ),
            signal_spacing_tilt_pct=float(os.getenv("GRID_SIGNAL_SPACING_TILT_PCT", "0.05")),
            signal_extra_levels_per_score=int(os.getenv("GRID_SIGNAL_EXTRA_LEVELS_PER_SCORE", "2")),
        )

    def order_quantity(
        self,
        price: float,
        rules: PairRules,
        order_notional_usd: float | None = None,
    ) -> float:
        notional = self.per_level_notional_usd if order_notional_usd is None else order_notional_usd
        if notional <= 0 or price <= 0:
            return 0.0
        return rules.round_amount(notional / price)
