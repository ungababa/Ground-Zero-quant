from __future__ import annotations

from dataclasses import dataclass

from src.config import GridConfig

TRACKED_PAIRS: tuple[str, ...] = ("ETH/USD", "SOL/USD", "BTC/USD")


@dataclass(slots=True)
class PortfolioSnapshot:
    balance: dict
    mid_by_pair: dict[str, float]
    change_24h_by_pair: dict[str, float]
    open_orders_by_pair: dict[str, list[dict]]
    pending_buy_notional_by_pair: dict[str, float]
    pending_sell_qty_by_pair: dict[str, float]
    usd_free: float
    effective_usd: float
    free_qty_by_asset: dict[str, float]
    effective_qty_by_asset: dict[str, float]
    total_equity_usd: float


@dataclass(slots=True)
class RuntimeSizing:
    pair: str
    asset: str
    total_equity_usd: float
    reserve_usd: float
    emergency_reserve_usd: float
    emergency_release_fraction: float
    emergency_locked_cash_usd: float
    pair_change_24h: float
    portfolio_drawdown_pct: float
    target_weight_pct: float
    target_notional_usd: float
    current_effective_qty: float
    current_notional_usd: float
    pending_buy_notional_usd: float
    investable_cap_notional_usd: float
    inventory_floor_notional_usd: float
    buy_gap_notional_usd: float
    sell_gap_notional_usd: float
    wallet_deployable_cash_usd: float
    pair_deployable_cash_usd: float
    runtime_buy_order_notional_usd: float
    runtime_sell_order_notional_usd: float
    buy_scale: float
    sell_scale: float
    buy_room_ratio: float
    sell_room_ratio: float


def asset_from_pair(pair: str) -> str:
    return pair.split("/")[0]


def pending_orders(order_response: dict) -> list[dict]:
    if not order_response.get("Success", True):
        return []
    return [o for o in order_response.get("OrderMatched", []) if o.get("Status") == "PENDING"]


def pending_buy_notional(orders: list[dict]) -> float:
    total = 0.0
    for order in orders:
        if str(order.get("Side", "")).upper() == "BUY":
            total += float(order.get("Price", 0.0)) * float(order.get("Quantity", 0.0))
    return total


def pending_sell_qty(orders: list[dict]) -> float:
    total = 0.0
    for order in orders:
        if str(order.get("Side", "")).upper() == "SELL":
            total += float(order.get("Quantity", 0.0))
    return total


def reserve_cash_usd(config: GridConfig, total_equity_usd: float) -> float:
    if config.reserve_cash_usd > 0:
        return config.reserve_cash_usd
    if total_equity_usd > 0:
        return total_equity_usd * config.cash_reserve_pct
    return 0.0


def release_fraction_for_conditions(
    *,
    pair_drop_pct: float,
    portfolio_drawdown_pct: float,
    config: GridConfig,
) -> float:
    pair_severity = 0.0
    if pair_drop_pct <= -config.crash_trigger_pct:
        pair_severity = abs(pair_drop_pct) / max(config.crash_trigger_pct, 1e-9) - 1.0

    dd_severity = 0.0
    if portfolio_drawdown_pct <= -config.portfolio_drawdown_trigger_pct:
        dd_severity = abs(portfolio_drawdown_pct) / max(config.portfolio_drawdown_trigger_pct, 1e-9) - 1.0

    severity = max(pair_severity, dd_severity, 0.0)
    if severity <= 0.0:
        return 0.0

    frac = config.emergency_release_frac + config.emergency_release_slope * severity
    return float(max(0.0, min(1.0, frac)))


def fetch_portfolio_snapshot(
    client,
    *,
    max_open_orders: int,
    own_ticker_mid: float | None = None,
    own_ticker_change_24h: float | None = None,
    own_pair: str | None = None,
    tracked_pairs: tuple[str, ...] = TRACKED_PAIRS,
) -> PortfolioSnapshot:
    balance = client.balance()
    wallet = balance.get("SpotWallet", {})

    mid_by_pair: dict[str, float] = {}
    change_24h_by_pair: dict[str, float] = {}
    open_orders_by_pair: dict[str, list[dict]] = {}
    pending_buy_notional_by_pair: dict[str, float] = {}
    pending_sell_qty_by_pair: dict[str, float] = {}

    for pair in tracked_pairs:
        if own_pair is not None and pair == own_pair and own_ticker_mid is not None:
            mid = own_ticker_mid
            change_24h = float(own_ticker_change_24h or 0.0)
        else:
            market = client.ticker(pair)["Data"][pair]
            mid = (float(market["MaxBid"]) + float(market["MinAsk"])) / 2.0
            change_24h = float(market.get("Change", 0.0))

        mid_by_pair[pair] = mid
        change_24h_by_pair[pair] = change_24h
        orders = pending_orders(client.query_orders(pair, pending_only=True, limit=max_open_orders))
        open_orders_by_pair[pair] = orders
        pending_buy_notional_by_pair[pair] = pending_buy_notional(orders)
        pending_sell_qty_by_pair[pair] = pending_sell_qty(orders)

    usd_free = float(wallet.get("USD", {}).get("Free", 0.0))
    effective_usd = usd_free + sum(pending_buy_notional_by_pair.values())

    free_qty_by_asset: dict[str, float] = {}
    effective_qty_by_asset: dict[str, float] = {}

    for pair in tracked_pairs:
        asset = asset_from_pair(pair)
        free_qty = float(wallet.get(asset, {}).get("Free", 0.0))
        free_qty_by_asset[asset] = free_qty
        effective_qty_by_asset[asset] = free_qty + pending_sell_qty_by_pair[pair]

    total_equity_usd = effective_usd
    for pair in tracked_pairs:
        asset = asset_from_pair(pair)
        total_equity_usd += effective_qty_by_asset[asset] * mid_by_pair[pair]

    return PortfolioSnapshot(
        balance=balance,
        mid_by_pair=mid_by_pair,
        change_24h_by_pair=change_24h_by_pair,
        open_orders_by_pair=open_orders_by_pair,
        pending_buy_notional_by_pair=pending_buy_notional_by_pair,
        pending_sell_qty_by_pair=pending_sell_qty_by_pair,
        usd_free=usd_free,
        effective_usd=effective_usd,
        free_qty_by_asset=free_qty_by_asset,
        effective_qty_by_asset=effective_qty_by_asset,
        total_equity_usd=total_equity_usd,
    )


def compute_runtime_sizing(
    config: GridConfig,
    snapshot: PortfolioSnapshot,
    pair: str,
    *,
    portfolio_drawdown_pct: float = 0.0,
) -> RuntimeSizing:
    asset = asset_from_pair(pair)
    mid = snapshot.mid_by_pair[pair]
    pair_change_24h = snapshot.change_24h_by_pair.get(pair, 0.0)

    target_weight = config.target_weight_pct
    if target_weight <= 0 and config.capital_base_usd > 0 and snapshot.total_equity_usd > 0:
        target_weight = config.capital_base_usd / snapshot.total_equity_usd

    if target_weight > 0:
        target_notional = snapshot.total_equity_usd * target_weight
    else:
        target_notional = config.capital_base_usd or config.base_max_position_notional_usd

    max_position_pct = config.max_position_pct_of_target if config.max_position_pct_of_target > 0 else 1.0
    investable_cap = max(target_notional * max_position_pct, 0.0)

    current_effective_qty = snapshot.effective_qty_by_asset.get(asset, 0.0)
    current_notional = current_effective_qty * mid
    own_pending_buy_notional = snapshot.pending_buy_notional_by_pair.get(pair, 0.0)

    base_reserve_usd = reserve_cash_usd(config, snapshot.total_equity_usd)
    emergency_reserve_usd = max(snapshot.total_equity_usd * config.emergency_reserve_pct, 0.0)
    emergency_release_fraction = release_fraction_for_conditions(
        pair_drop_pct=pair_change_24h,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
        config=config,
    )
    locked_emergency_cash = emergency_reserve_usd * (1.0 - emergency_release_fraction)

    wallet_deployable_cash = max(snapshot.usd_free - base_reserve_usd - locked_emergency_cash, 0.0)
    pair_deployable_cash = wallet_deployable_cash + own_pending_buy_notional

    committed_buy_side_notional = current_notional + own_pending_buy_notional
    buy_gap = max(investable_cap - committed_buy_side_notional, 0.0)

    inventory_floor_notional = max(target_notional * config.min_inventory_floor_pct_of_target, 0.0)
    sell_gap = max(current_notional - inventory_floor_notional, 0.0)

    preferred_order_notional = (
        target_notional * config.order_size_pct_of_target
        if config.order_size_pct_of_target > 0
        else config.base_per_level_notional_usd
    )
    levels = max(config.adaptive_order_levels, 1)

    buy_room_ratio = buy_gap / investable_cap if investable_cap > 0 else 0.0
    buy_scale = config.buy_room_min_mult + (
        config.buy_room_max_mult - config.buy_room_min_mult
    ) * (buy_room_ratio ** config.inventory_room_power)
    buy_scale = float(max(config.buy_room_min_mult, min(config.buy_room_max_mult, buy_scale)))

    sell_room_denom = max(investable_cap - inventory_floor_notional, 1e-9)
    sell_room_ratio = sell_gap / sell_room_denom if sell_room_denom > 0 else 0.0
    sell_scale = config.sell_room_min_mult + (
        config.sell_room_max_mult - config.sell_room_min_mult
    ) * (sell_room_ratio ** config.inventory_room_power)
    sell_scale = float(max(config.sell_room_min_mult, min(config.sell_room_max_mult, sell_scale)))

    runtime_buy_order_notional = min(preferred_order_notional * buy_scale, buy_gap / levels) if buy_gap > 0 else 0.0
    runtime_sell_order_notional = min(preferred_order_notional * sell_scale, sell_gap / levels) if sell_gap > 0 else 0.0

    return RuntimeSizing(
        pair=pair,
        asset=asset,
        total_equity_usd=snapshot.total_equity_usd,
        reserve_usd=base_reserve_usd,
        emergency_reserve_usd=emergency_reserve_usd,
        emergency_release_fraction=emergency_release_fraction,
        emergency_locked_cash_usd=locked_emergency_cash,
        pair_change_24h=pair_change_24h,
        portfolio_drawdown_pct=portfolio_drawdown_pct,
        target_weight_pct=target_weight,
        target_notional_usd=target_notional,
        current_effective_qty=current_effective_qty,
        current_notional_usd=current_notional,
        pending_buy_notional_usd=own_pending_buy_notional,
        investable_cap_notional_usd=investable_cap,
        inventory_floor_notional_usd=inventory_floor_notional,
        buy_gap_notional_usd=buy_gap,
        sell_gap_notional_usd=sell_gap,
        wallet_deployable_cash_usd=wallet_deployable_cash,
        pair_deployable_cash_usd=pair_deployable_cash,
        runtime_buy_order_notional_usd=runtime_buy_order_notional,
        runtime_sell_order_notional_usd=runtime_sell_order_notional,
        buy_scale=buy_scale,
        sell_scale=sell_scale,
        buy_room_ratio=buy_room_ratio,
        sell_room_ratio=sell_room_ratio,
    )
