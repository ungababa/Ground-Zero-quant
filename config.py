import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


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
class MeanReversionConfig:
    pair: str = "BTC/USD"
    mean_window: int = 24
    zscore_entry: float = 2.0
    zscore_exit: float = 0.5
    position_notional_usd: float = 1000.0
    max_position_notional_usd: float = 10000.0
    max_24h_abs_change_pct: float = 0.2
    pause_guard: bool = True
    max_open_orders: int = 20

    @classmethod
    def from_env(cls) -> "MeanReversionConfig":
        return cls(
            pair=os.getenv("MR_PAIR", "BTC/USD"),
            mean_window=int(os.getenv("MR_MEAN_WINDOW", "24")),
            zscore_entry=float(os.getenv("MR_ZSCORE_ENTRY", "2.0")),
            zscore_exit=float(os.getenv("MR_ZSCORE_EXIT", "0.5")),
            position_notional_usd=float(os.getenv("MR_POSITION_NOTIONAL_USD", "1000")),
            max_position_notional_usd=float(os.getenv("MR_MAX_POSITION_NOTIONAL_USD", "10000")),
            max_24h_abs_change_pct=float(os.getenv("MR_MAX_24H_ABS_CHANGE_PCT", "0.2")),
            pause_guard=os.getenv("MR_PAUSE_GUARD", "true").lower() == "true",
            max_open_orders=int(os.getenv("MR_MAX_OPEN_ORDERS", "20")),
        )

    def order_quantity(self, price: float, rules: PairRules) -> float:
        if price <= 0:
            raise ValueError("Price must be positive")
        quantity = self.position_notional_usd / price
        quantity = rules.round_amount(quantity)
        return quantity
