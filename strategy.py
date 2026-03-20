import logging
from dataclasses import dataclass
from typing import Iterable

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
        log.debug("GridStrategy created | rules=%s", rules)

    def should_pause(self, ticker: TickerView) -> bool:
        result = abs(ticker.change_24h) >= self.config.max_24h_abs_change_pct
        log.debug(
            "should_pause? abs(change_24h)=%.4f%% vs threshold=%.4f%% => %s",
            abs(ticker.change_24h) * 100,
            self.config.max_24h_abs_change_pct * 100,
            result,
        )
        return result

    def should_refresh(self, ticker: TickerView) -> bool:
        if self.anchor_price is None:
            log.debug("should_refresh? anchor_price is None => True (first tick)")
            return True
        drift = abs(ticker.mid - self.anchor_price) / self.anchor_price
        result = drift >= self.config.refresh_threshold_pct
        log.debug(
            "should_refresh? mid=%.2f  anchor=%.2f  drift=%.4f%%  threshold=%.4f%% => %s",
            ticker.mid,
            self.anchor_price,
            drift * 100,
            self.config.refresh_threshold_pct * 100,
            result,
        )
        return result

    def set_anchor(self, price: float) -> None:
        old = self.anchor_price
        self.anchor_price = price
        log.info("ANCHOR SET  %.2f -> %.2f", old or 0.0, price)

    def desired_orders(self, ticker: TickerView, coin_position: float) -> list[DesiredOrder]:
        anchor = self.anchor_price or ticker.mid
        qty = self.config.order_quantity(anchor, self.rules)
        max_coin = self.config.max_position_notional_usd / ticker.mid
        log.debug(
            "desired_orders | anchor=%.2f  qty_per_level=%.8f  coin_position=%.8f  "
            "max_coin=%.8f  levels_per_side=%d  spacing_pct=%.4f%%",
            anchor,
            qty,
            coin_position,
            max_coin,
            self.config.levels_per_side,
            self.config.spacing_pct * 100,
        )
        orders: list[DesiredOrder] = []

        for level in range(1, self.config.levels_per_side + 1):
            buy_price = self.rules.round_price(anchor * (1 - self.config.spacing_pct * level))
            sell_price = self.rules.round_price(anchor * (1 + self.config.spacing_pct * level))

            projected_coin = coin_position + qty * level
            buy_ok = projected_coin <= max_coin
            sell_ok = round(coin_position - qty * level, 8) >= 0

            log.debug(
                "  level=%d  buy_price=%.2f (projected_coin=%.8f, ok=%s)  sell_price=%.2f (remaining=%.8f, ok=%s)",
                level,
                buy_price,
                projected_coin,
                buy_ok,
                sell_price,
                coin_position - qty * level,
                sell_ok,
            )

            if buy_ok:
                orders.append(DesiredOrder("BUY", buy_price, qty))
            if sell_ok:
                orders.append(DesiredOrder("SELL", sell_price, qty))

        pre_filter = len(orders)
        orders = [order for order in orders if order.price * order.quantity >= self.rules.min_order_value]
        if pre_filter != len(orders):
            log.debug(
                "  min_order_value filter removed %d orders (min=%.2f)",
                pre_filter - len(orders),
                self.rules.min_order_value,
            )

        log.debug("desired_orders result (%d orders):", len(orders))
        for i, o in enumerate(orders):
            log.debug("  [%d] %s", i, o)
        return orders

    def equivalent(self, live_orders: Iterable[dict], desired_orders: Iterable[DesiredOrder]) -> bool:
        live = sorted(
            (
                order["Side"],
                round(float(order["Price"]), 8),
                round(float(order["Quantity"]), 8),
            )
            for order in live_orders
        )
        desired = sorted((order.side, round(order.price, 8), round(order.quantity, 8)) for order in desired_orders)
        result = live == desired
        log.debug(
            "equivalent? live_count=%d  desired_count=%d  match=%s",
            len(live),
            len(desired),
            result,
        )
        if not result:
            log.debug("  live   = %s", live)
            log.debug("  desired= %s", desired)
        return result
