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
    pair: str = "BTC/USD"
    levels_per_side: int = 4
    spacing_pct: float = 0.0035
    per_level_notional_usd: float = 2000.0
    max_position_notional_usd: float = 12000.0
    refresh_threshold_pct: float = 0.003
    poll_seconds: int = 60
    max_open_orders: int = 8
    max_24h_abs_change_pct: float = 0.03
    reanchor_after_fill: bool = True

    @classmethod
    def from_env(cls) -> "GridConfig":
        return cls(
            pair=os.getenv("GRID_PAIR", "BTC/USD"),
            levels_per_side=int(os.getenv("GRID_LEVELS_PER_SIDE", "4")),
            spacing_pct=float(os.getenv("GRID_SPACING_PCT", "0.0035")),
            per_level_notional_usd=float(
                os.getenv("GRID_PER_LEVEL_NOTIONAL_USD", "2000")
            ),
            max_position_notional_usd=float(
                os.getenv("GRID_MAX_POSITION_NOTIONAL_USD", "12000")
            ),
            refresh_threshold_pct=float(
                os.getenv("GRID_REFRESH_THRESHOLD_PCT", "0.003")
            ),
            poll_seconds=int(os.getenv("GRID_POLL_SECONDS", "20")),
            max_open_orders=int(os.getenv("GRID_MAX_OPEN_ORDERS", "8")),
            max_24h_abs_change_pct=float(
                os.getenv("GRID_MAX_24H_ABS_CHANGE_PCT", "0.03")
            ),
            reanchor_after_fill=os.getenv("GRID_REANCHOR_AFTER_FILL", "true").lower()
            == "true",
        )

    def order_quantity(self, price: float, rules: PairRules) -> float:
        return rules.round_amount(self.per_level_notional_usd / price)
