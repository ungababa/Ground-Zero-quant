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

    def __repr__(self) -> str:
        return (
            f"PairRules(pair={self.pair!r}, price_prec={self.price_precision}, "
            f"amount_prec={self.amount_precision}, min_order={self.min_order_value})"
        )


@dataclass(slots=True)
class GridConfig:
    pair: str = "ETH/USD"
    levels_per_side: int = 30
    spacing_pct: float = 0.01
    per_level_notional_usd: float = 15000.0
    max_position_notional_usd: float = 450000.0
    refresh_threshold_pct: float = 0.05
    poll_seconds: int = 60
    max_open_orders: int = 64
    max_24h_abs_change_pct: float = 0.12
    reanchor_after_fill: bool = True
    pause_guard: bool = True

    enable_signal_tilt: bool = True
    signal_history_limit: int = 200
    signal_fast_window: int = 8
    signal_slow_window: int = 24
    signal_bb_window: int = 20
    signal_bb_std_mult: float = 2.0
    signal_band_touch_tolerance_pct: float = 0.002
    signal_spacing_tilt_pct: float = 0.10
    signal_extra_levels_per_score: int = 2

    @classmethod
    def from_env(cls) -> "GridConfig":
        return cls(
            pair=os.getenv("GRID_PAIR", "ETH/USD"),
            levels_per_side=int(os.getenv("GRID_LEVELS_PER_SIDE", "30")),
            spacing_pct=float(os.getenv("GRID_SPACING_PCT", "0.01")),
            per_level_notional_usd=float(os.getenv("GRID_PER_LEVEL_NOTIONAL_USD", "15000")),
            max_position_notional_usd=float(os.getenv("GRID_MAX_POSITION_NOTIONAL_USD", "450000")),
            refresh_threshold_pct=float(os.getenv("GRID_REFRESH_THRESHOLD_PCT", "0.05")),
            poll_seconds=int(os.getenv("GRID_POLL_SECONDS", "60")),
            max_open_orders=int(os.getenv("GRID_MAX_OPEN_ORDERS", "64")),
            max_24h_abs_change_pct=float(os.getenv("GRID_MAX_24H_ABS_CHANGE_PCT", "0.12")),
            reanchor_after_fill=os.getenv("GRID_REANCHOR_AFTER_FILL", "true").lower() == "true",
            pause_guard=os.getenv("GRID_PAUSE_GUARD", "true").lower() == "true",
            enable_signal_tilt=os.getenv("GRID_ENABLE_SIGNAL_TILT", "true").lower() == "true",
            signal_history_limit=int(os.getenv("GRID_SIGNAL_HISTORY_LIMIT", "200")),
            signal_fast_window=int(os.getenv("GRID_SIGNAL_FAST_WINDOW", "8")),
            signal_slow_window=int(os.getenv("GRID_SIGNAL_SLOW_WINDOW", "24")),
            signal_bb_window=int(os.getenv("GRID_SIGNAL_BB_WINDOW", "20")),
            signal_bb_std_mult=float(os.getenv("GRID_SIGNAL_BB_STD_MULT", "2.0")),
            signal_band_touch_tolerance_pct=float(
                os.getenv("GRID_SIGNAL_BAND_TOUCH_TOLERANCE_PCT", "0.002")
            ),
            signal_spacing_tilt_pct=float(os.getenv("GRID_SIGNAL_SPACING_TILT_PCT", "0.10")),
            signal_extra_levels_per_score=int(os.getenv("GRID_SIGNAL_EXTRA_LEVELS_PER_SCORE", "2")),
        )

    def order_quantity(self, price: float, rules: PairRules) -> float:
        return rules.round_amount(self.per_level_notional_usd / price)