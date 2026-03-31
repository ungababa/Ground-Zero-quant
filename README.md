# Portfolio-Aware Dynamic Grid Bot

This version is the portfolio-aware upgrade of the original 3-bot Roostoo combo.

There are still **3 bots only**:

- ETH/USD bot
- SOL/USD bot
- BTC/USD bot

All 3 bots share **one wallet**.

The key difference from the old version is that the bot no longer sizes itself from a blind fixed-dollar sleeve only. It now uses the **current total portfolio value** and the **current live inventory** to decide how much it can still buy and how much it can reasonably sell.

---

## Main idea

Each bot should behave as one sleeve of one shared portfolio.

Instead of thinking:

- ETH bot has its own isolated money
- SOL bot has its own isolated money
- BTC bot has its own isolated money

this version thinks:

- there is **one wallet**
- the wallet contains USD + ETH + SOL + BTC
- each bot should size itself as a percentage of that **total live portfolio**

So for each cycle, the bot computes:

- total wallet equity
- its target sleeve value
- its current asset value
- how far under target it is on the buy side
- how much inventory above a floor it can recycle on the sell side

---

## What is included in total portfolio value

Live deployment uses:

- free USD
- free ETH/SOL/BTC
- pending buy notionals
- pending sell quantities

So the bot sees not just free balances, but also what is already committed in orders.

That is what gives the “aware of the other bots” behavior without needing direct bot-to-bot communication.

---

## Strategy behavior

This is still a **grid / mean-reversion strategy**.

It is **not** a stop-loss trend-following bot.

That means:

- it buys lower through the grid
- it sells higher through the grid
- it tries to stay close to the target sleeve structure
- it does **not** panic sell just because price keeps going down

So in a straight one-way crash:

- it may keep accumulating lower
- it may eventually stop buying if buy gap / deployable cash is exhausted
- it does **not** automatically cut losses like a stop-loss system

---

## Current tuned parameters

These are the tuned defaults baked into this version:

- `levels_per_side = 25`
- `spacing_pct = 0.011`
- `buy_spacing_multiplier = 1.0`
- `sell_spacing_multiplier = 1.0`
- `order_size_pct_of_target = 0.022`
- `min_inventory_floor_pct_of_target = 0.10`
- `adaptive_order_levels = 10`
- `cash_reserve_pct = 0.07`
- `refresh_threshold_pct = 0.05`
- `signal_spacing_tilt_pct = 0.05`

These are chosen to improve upside participation without losing the allocation discipline from the portfolio-aware framework.

---

## Recommended target weights

Use these target weights across the 3 bots:

- ETH/USD: `0.50`
- SOL/USD: `0.25`
- BTC/USD: `0.25`

You can provide this in `.env` using `GRID_TARGET_WEIGHT_PCT`.

---

## Recommended `.env` examples

### ETH bot
```env
GRID_PAIR=ETH/USD
GRID_TARGET_WEIGHT_PCT=0.50
GRID_ORDER_SIZE_PCT_OF_TARGET=0.022
GRID_MAX_POSITION_PCT_OF_TARGET=1.0
GRID_MIN_INVENTORY_FLOOR_PCT_OF_TARGET=0.10
GRID_ADAPTIVE_ORDER_LEVELS=10
GRID_LEVELS_PER_SIDE=25
GRID_SPACING_PCT=0.011
GRID_BUY_SPACING_MULTIPLIER=1.0
GRID_SELL_SPACING_MULTIPLIER=1.0
GRID_CASH_RESERVE_PCT=0.07
GRID_REFRESH_THRESHOLD_PCT=0.05
GRID_SIGNAL_SPACING_TILT_PCT=0.05