# Adaptive Inventory-Aware Crypto Grid

## Overview

This repo contains our final **3-bot crypto grid strategy** for the Roostoo competition.

We are running **one bot per asset**:
- **ETH/USD**
- **SOL/USD**
- **BTC/USD**

The strategy is designed for a **short competition window (~10 days)**, so the final setup prioritizes:

1. **Return first**
2. Then **Sharpe / Sortino / Calmar**
3. Then reasonable execution safety and inventory control

---

## Final Portfolio Allocation

We are using:

- **ETH: 50%**
- **SOL: 25%**
- **BTC: 25%**

For total capital of **$1,000,000**, that means:

- **ETH bot capital base:** $500,000
- **SOL bot capital base:** $250,000
- **BTC bot capital base:** $250,000

---

## Final Shared Strategy Parameters

These are the **same across all 3 bots**:

- `GRID_SPACING_PCT = 0.011`
- `GRID_BUY_SPACING_MULTIPLIER = 1.2`
- `GRID_SELL_SPACING_MULTIPLIER = 1.0`
- `GRID_NEAREST_BUY_OFFSET_MULTIPLIER = 1.0`
- `GRID_NEAREST_SELL_OFFSET_MULTIPLIER = 1.0`
- `GRID_LEVELS_PER_SIDE = 35`
- `GRID_REANCHOR_AFTER_FILL = true`
- `GRID_PAUSE_GUARD = true`
- `GRID_CASH_RESERVE_PCT = 0.05`
- `GRID_DISABLE_DOWNWARD_REFRESH_WHEN_NO_CASH = true`
- `GRID_DISABLE_UPWARD_REFRESH_WHEN_NO_COIN = true`

---

## Final Per-Bot Order Size

We size each bot’s order notional to about **1.7% of that bot’s capital**.

### ETH bot
- capital base: **$500,000**
- reserve cash: **$25,000**
- max position notional: **$475,000**
- per-level notional: **$8,500**

### SOL bot
- capital base: **$250,000**
- reserve cash: **$12,500**
- max position notional: **$237,500**
- per-level notional: **$4,250**

### BTC bot
- capital base: **$250,000**
- reserve cash: **$12,500**
- max position notional: **$237,500**
- per-level notional: **$4,250**

---

## What the Strategy Does

This is a **grid / mean-reversion bot**.

It is designed to:
- place buys below the anchor
- place sells above the anchor
- re-anchor after fills
- harvest repeated oscillations / rebounds
- avoid overcommitting once one side of inventory is exhausted

It is **not** a trend-following bot.

It is **not** a stop-loss bot.

It performs best in:
- choppy markets
- two-way markets
- rebound / mean-reverting conditions

It performs worse in:
- one-way crashes
- runaway rallies with no pullback
- extremely strong trend markets

---

## Order Geometry

### Sell ladder
With:
- `GRID_SPACING_PCT = 0.011`
- `GRID_SELL_SPACING_MULTIPLIER = 1.0`

The first sell is approximately:

- **+1.10% above the anchor**

Then:
- second sell: **+2.20%**
- third sell: **+3.30%**
- etc.

### Buy ladder
With:
- `GRID_SPACING_PCT = 0.011`
- `GRID_BUY_SPACING_MULTIPLIER = 1.2`

The first buy is approximately:

- **-1.32% below the anchor**

Then:
- second buy: **-2.64%**
- third buy: **-3.96%**
- etc.

### What this means
- buys are placed a bit more patiently
- sells are monetized a bit sooner
- this helps reduce overbuying while still taking rebounds

---

## How Re-Anchoring Works

We use:

- `GRID_REANCHOR_AFTER_FILL = true`

That means after a fill:
- if a **buy** fills, anchor resets to that buy price
- if a **sell** fills, anchor resets to that sell price

This keeps the grid relevant to the latest executed market level.

---

## Inventory / Capital Exhaustion Logic

One of the biggest upgrades in this version is **inventory-aware behavior**.

### When cash is exhausted
If deployable cash is gone:
- the bot **stops placing new buys**
- it becomes effectively **sell-only**
- it waits for rebound sells
- it does **not** keep refreshing anchor downward if fully invested

This is controlled by:
- `GRID_CASH_RESERVE_PCT`
- `GRID_DISABLE_DOWNWARD_REFRESH_WHEN_NO_CASH = true`

### When coin is exhausted
If coin inventory is gone:
- the bot **stops placing new sells**
- it becomes effectively **buy-only**
- it waits for pullback re-entry
- it does **not** keep refreshing anchor upward if fully exited

This is controlled by:
- `GRID_DISABLE_UPWARD_REFRESH_WHEN_NO_COIN = true`

### Why this matters
Without this logic, a grid bot can:
- keep trying to place unfundable buys
- keep dragging anchor the wrong way
- degrade trade quality badly when one-sided

This final version prevents that.

---

## Pause / Protection Logic

We use:
- `GRID_PAUSE_GUARD = true`

and a 24h move threshold:
- `GRID_MAX_24H_ABS_CHANGE_PCT = 0.12`

If the market moves too hard in 24h, the bot can pause instead of blindly continuing.

Important:
- this is **not** a stop-loss
- it is a **volatility/trend guard**

---

## Signal Tilt Logic

The bot still supports signal tilt:
- fast MA
- slow MA
- Bollinger-style band touches

These affect:
- spacing tilt
- extra levels depending on bullish/bearish score

But the final recommended setup is still primarily driven by:
- spacing
- levels
- order size
- capital allocation
- inventory-aware execution

---

## What Changed From the Original Version

### 1. Portfolio allocation changed
Original setup was closer to:
- **ETH 45% / SOL 35% / BTC 20%**

Final setup:
- **ETH 50% / SOL 25% / BTC 25%**

Reason:
- stronger recent return profile
- better ratios
- less overexposure to SOL

---

### 2. Grid spacing changed
Original baseline:
- `GRID_SPACING_PCT = 0.010`

Final:
- `GRID_SPACING_PCT = 0.011`

Reason:
- slightly wider grid reduced churn
- improved recent competition-window behavior

---

### 3. Buy/sell spacing asymmetry changed
Original baseline:
- symmetric `1.0 / 1.0`

Final:
- `GRID_BUY_SPACING_MULTIPLIER = 1.2`
- `GRID_SELL_SPACING_MULTIPLIER = 1.0`

Reason:
- buys are slightly more patient
- reduces over-averaging on dips
- still takes rebound sells at a cleaner distance

---

### 4. Levels increased
Original baseline:
- `GRID_LEVELS_PER_SIDE = 30`

Final:
- `GRID_LEVELS_PER_SIDE = 35`

Reason:
- better inventory ladder coverage

---

### 5. Order size increased
Original baseline backtest region:
- smaller per-level notional

Final:
- about **1.7% of each bot’s capital per order**

Reason:
- stronger recent return profile
- better monetization per fill

---

### 6. Independent nearest offset support was added
We added:
- `GRID_NEAREST_BUY_OFFSET_MULTIPLIER`
- `GRID_NEAREST_SELL_OFFSET_MULTIPLIER`

These allow independent control of the nearest opposite-side order distance.

In the final chosen deployment, both are:
- `1.0`

So the strategy currently keeps nearest offsets neutral.

---

### 7. Inventory-aware live execution was added
This is a major improvement.

The live bot now:
- tracks reserve cash
- calculates deployable cash
- blocks new buys when cash is exhausted
- blocks new sells when coin is exhausted
- blocks adverse-direction refresh when inventory is locked

This was not fully present in the original live logic.

---

## Why We Use One Common Parameter Set Across All 3 Bots

We considered whether each coin should have its own completely different parameter set.

Final conclusion:
- **same strategy parameters**
- **different capital allocations**
- **different per-order USD size**

Reason:
- the best evidence came from **portfolio-level sweeps**
- this is simpler operationally
- easier to deploy
- less likely to overfit
- fewer live failure points

So:
- the geometry is the same
- the sizing is different because capital is different

---

## Files Updated

### Live deployment files
These need to be updated for live usage:
- `main.py`
- `config.py`
- `strategy.py`
- bot-specific `.env` files

### Backtest / research files
These are for testing only:
- `backtest.py`
- sweeps / optimization scripts

---

## Final Live Env Values

### ETH bot
- pair: `ETH/USD`
- capital base: `500000`
- reserve cash: `25000`
- max position notional: `475000`
- per-level notional: `8500`

### SOL bot
- pair: `SOL/USD`
- capital base: `250000`
- reserve cash: `12500`
- max position notional: `237500`
- per-level notional: `4250`

### BTC bot
- pair: `BTC/USD`
- capital base: `250000`
- reserve cash: `12500`
- max position notional: `237500`
- per-level notional: `4250`

All other shared strategy parameters are identical across the 3 bots.

---

## Expected Bot Behavior by Scenario

### 1. Sideways market
Best-case regime.
- bot buys dips
- bot sells rebounds
- repeated harvesting of small oscillations

### 2. Market goes down but cash remains
- successive buys can fill lower
- average cost improves
- bot becomes more invested
- waits for rebound sells later

### 3. Market keeps going down after cash is exhausted
- no more new buys
- bot becomes sell-only
- waits for bounce
- no downward refresh while buy-locked

### 4. Market goes up while coin remains
- successive sells fill
- realized pnl accumulates
- cash increases

### 5. Market keeps going up after coin is exhausted
- no more new sells
- bot becomes buy-only
- waits for pullback re-entry
- no upward refresh while sell-locked

### 6. Violent market move
- pause guard can trigger
- bot avoids blindly continuing in extreme moves

---

## Important Limitations

This strategy does **not**:
- hard stop-loss liquidate
- trend-follow breakouts
- force de-risk in long one-way moves
- guarantee protection in all market regimes

This strategy **does**:
- behave sensibly when one side is exhausted
- avoid overcommitting capital
- harvest mean reversion
- optimize for the competition’s short horizon

---

## Why This Final Setup Was Chosen

The final setup was selected by comparing:
- 60-day sweeps
- 21-day sweeps
- TP/offset sweeps
- return-first portfolio ranking
- Sharpe / Sortino / Calmar quality checks
- recent-window relevance for a ~10-day competition

Final conclusion:
- the best competition-ready version is **not** the most exotic or overfit one
- it is the version that gave:
  - strong recent return
  - cleaner ratios
  - sensible live execution behavior
  - simpler deployment structure

---

## Final Deployment Summary

### Use this across all bots
- spacing: `0.011`
- buy multiplier: `1.2`
- sell multiplier: `1.0`
- nearest buy offset: `1.0`
- nearest sell offset: `1.0`
- levels per side: `35`
- reserve cash: `5%`
- reanchor after fill: `true`
- pause guard: `true`

### Use these weights
- **ETH 50%**
- **SOL 25%**
- **BTC 25%**

### Use these per-level notionals
- **ETH:** `8500`
- **SOL:** `4250`
- **BTC:** `4250`

---

## Final One-Line Strategy Description

A **3-bot inventory-aware crypto grid strategy** optimized for a short competition window, using shared grid geometry, asymmetric patience on buys, portfolio-weighted sizing, reserve cash protection, and one-sided behavior when capital or coin inventory is exhausted.