from dataclasses import dataclass
from typing import Iterable

from config import GridConfig, PairRules


@dataclass(slots=True)
class DesiredOrder:
    side: str
    price: float
    quantity: float


@dataclass(slots=True)
class TickerView:
    bid: float
    ask: float
    last: float
    change_24h: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class GridStrategy:
    def __init__(self, config: GridConfig, rules: PairRules) -> None:
        self.config = config
        self.rules = rules
        self.anchor_price: float | None = None

    def should_pause(self, ticker: TickerView) -> bool:
        return abs(ticker.change_24h) >= self.config.max_24h_abs_change_pct

    def should_refresh(self, ticker: TickerView) -> bool:
        if self.anchor_price is None:
            return True
        return (
            abs(ticker.mid - self.anchor_price) / self.anchor_price
            >= self.config.refresh_threshold_pct
        )

    def set_anchor(self, price: float) -> None:
        self.anchor_price = price

    def desired_orders(
        self, ticker: TickerView, btc_position: float
    ) -> list[DesiredOrder]:
        anchor = self.anchor_price or ticker.mid
        qty = self.config.order_quantity(anchor, self.rules)
        orders: list[DesiredOrder] = []
        max_btc = self.config.max_position_notional_usd / ticker.mid

        for level in range(1, self.config.levels_per_side + 1):
            buy_price = self.rules.round_price(
                anchor * (1 - self.config.spacing_pct * level)
            )
            sell_price = self.rules.round_price(
                anchor * (1 + self.config.spacing_pct * level)
            )

            projected_btc = btc_position + qty * level
            if projected_btc <= max_btc:
                orders.append(DesiredOrder("BUY", buy_price, qty))

            if btc_position - qty * level >= -max_btc:
                orders.append(DesiredOrder("SELL", sell_price, qty))

        return [
            order
            for order in orders
            if order.price * order.quantity >= self.rules.min_order_value
        ]

    def equivalent(
        self, live_orders: Iterable[dict], desired_orders: Iterable[DesiredOrder]
    ) -> bool:
        live = sorted(
            (
                order["Side"],
                round(float(order["Price"]), 8),
                round(float(order["Quantity"]), 8),
            )
            for order in live_orders
        )
        desired = sorted(
            (order.side, round(order.price, 8), round(order.quantity, 8))
            for order in desired_orders
        )
        return live == desired
