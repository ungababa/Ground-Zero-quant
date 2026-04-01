# Trial 1285 — Portfolio-Aware Dynamic Grid Bot

This README describes the selected live deployment version of the portfolio-aware Roostoo grid bot after the focused refinement sweep around the earlier Trial 620 region.

## Version summary

**Selected version:** Trial 1285  
**Style:** Portfolio-aware dynamic grid / mean-reversion bot  
**Deployment model:** 3 bots sharing one wallet  
**Pairs:** ETH/USD, SOL/USD, BTC/USD  
**Target weights:**
- ETH/USD: **50%**
- SOL/USD: **25%**
- BTC/USD: **25%**

This version was chosen because it improved the main practical outcomes versus the previous base dynamic bot:
- better overall return behavior
- materially lower drawdown
- much better recent-window downside control
- lower overdeployment in weak/choppy markets

It is still a grid bot, not a stop-loss trend follower.

---

## Core idea

All 3 bots trade from **one shared wallet**.

Instead of treating ETH, SOL, and BTC as isolated sleeves with isolated cash, this version reads the portfolio as one combined account:
- free USD
- free ETH/SOL/BTC inventory
- pending buy orders
- pending sell orders

Each bot sizes itself using:
- total wallet equity
- its target sleeve weight
- current live inventory
- remaining buy room / sell room
- base cash reserve
- emergency reserve
- shared portfolio drawdown

So the strategy is aware of what the **other bots have already used**, not just its own pair.

---

## What changed versus the earlier basic dynamic bot

The earlier basic dynamic version was too exposed. It tended to keep deploying capital too aggressively when markets were soft.

Trial 1285 adds and/or emphasizes:
- **higher normal cash reserve**
- **separate emergency reserve**
- **reserve release only under real stress**
- **inventory-room-aware sizing**
- **more selective buying**
- **fewer adaptive levels**, so it is less eager to keep adding exposure

This means:
- it still buys dips and sells strength
- but it does so more conservatively
- and it avoids using too much cash too early

---

## Exact selected parameters

### Strategy / sizing parameters
- `order_size_pct_of_target = 0.022`
- `cash_reserve_pct = 0.12`
- `emergency_reserve_pct = 0.08`
- `crash_trigger_pct = 0.08`
- `portfolio_drawdown_trigger_pct = 0.08`
- `emergency_release_frac = 0.60`
- `emergency_release_slope = 1.25`
- `adaptive_order_levels = 4`
- `levels_per_side = 20`
- `spacing_pct = 0.011`
- `buy_spacing_multiplier = 1.10`
- `sell_spacing_multiplier = 1.00`
- `min_inventory_floor_pct_of_target = 0.08`
- `inventory_room_power = 1.0`
- `buy_room_min_mult = 0.60`
- `buy_room_max_mult = 1.00`
- `sell_room_min_mult = 0.75`
- `sell_room_max_mult = 1.15`
- `signal_spacing_tilt_pct = 0.05`
- `signal_extra_levels_per_score = 2`

### Interpretation of the selected parameters

#### 1. More cash is held back
- `cash_reserve_pct = 0.12`
- `emergency_reserve_pct = 0.08`

The bot does not commit all deployable cash immediately. A portion remains untouched during normal conditions, and another portion is only released when stress is real.

#### 2. Buying is more selective
- `buy_spacing_multiplier = 1.10`
- `buy_room_min_mult = 0.60`
- `buy_room_max_mult = 1.00`

Buys are spaced slightly wider and buy sizing scales down when inventory room gets tighter.

#### 3. Ladder aggression is reduced
- `adaptive_order_levels = 4`
- `levels_per_side = 20`

The bot is still active, but it does not keep stacking exposure as aggressively as the base dynamic version.

#### 4. Reserve release is stress-based
- `crash_trigger_pct = 0.08`
- `portfolio_drawdown_trigger_pct = 0.08`
- `emergency_release_frac = 0.60`
- `emergency_release_slope = 1.25`

Emergency reserve is not supposed to be used during normal noise. It becomes available only when the pair is down sharply or the total shared portfolio is in meaningful drawdown.

---

## Why Trial 1285 was selected after testing

This version came from a focused local sweep around the earlier strong candidate region. The sweep trend was very clear:

The better configs consistently leaned toward:
- higher cash reserve
- meaningful emergency reserve
- lower buy aggressiveness
- fewer adaptive levels
- inventory-room-aware throttling

That trend means the edge did **not** come from radically changing the grid concept. It came from controlling exposure better.

---

## Performance summary versus the earlier base dynamic bot

## 21-day comparison

### Earlier base dynamic bot
- Return: **-1.57%**
- Max drawdown: **-11.57%**
- Sharpe: **-0.450**
- Sortino: **-0.628**
- Calmar: **-2.122**
- Upside capture: **0.798**
- Downside capture: **0.788**
- Avg cash: **20.17%**
- Trades/day: **17.3**

### Trial 1285
- Return: **+0.33%**
- Max drawdown: **-7.81%**
- Sharpe: **+0.345**
- Sortino: **+0.492**
- Calmar: **+0.774**
- Upside capture: **0.578**
- Downside capture: **0.557**
- Avg cash: **43.36%**
- Trades/day: **16.1**

### 21-day improvement
- Return: **+1.90 percentage points**
- Max drawdown: **3.77 points better**
- Sharpe: **+0.795**
- Sortino: **+1.120**
- Calmar: **+2.896**

This is a strong improvement.

---

## 60-day comparison

### Earlier base dynamic bot
- Return: **-19.60%**
- Max drawdown: **-27.61%**
- Sharpe: **-2.344**
- Sortino: **-2.955**
- Calmar: **-2.673**
- Upside capture: **0.719**
- Downside capture: **0.722**
- Avg cash: **29.69%**
- Trades/day: **26.0**

### Trial 1285
- Return: **-16.07%**
- Max drawdown: **-22.16%**
- Sharpe: **-3.317**
- Sortino: **-3.679**
- Calmar: **-2.973**
- Upside capture: **0.365**
- Downside capture: **0.385**
- Avg cash: **65.33%**
- Trades/day: **24.7**

### 60-day improvement
- Return: **+3.53 percentage points**
- Max drawdown: **5.44 points better**

### Important note
On 60 days, this version is **better on return and drawdown**, but **worse on Sharpe / Sortino / Calmar** in the backtest output. That means the chosen version is optimized more for practical capital preservation and lower damage than for maximizing every ratio metric.

---

## Recent behavior inside the 60-day run

### Earlier base dynamic bot
- Trailing 21d return: **-1.49%**
- Trailing 21d drawdown: **-8.93%**
- Trailing 10d return: **-4.12%**
- Trailing 10d drawdown: **-6.47%**
- Trailing 4d return: **+0.15%**
- Trailing 4d drawdown: **-2.21%**

### Trial 1285
- Trailing 21d return: **-0.63%**
- Trailing 21d drawdown: **-3.43%**
- Trailing 10d return: **-1.65%**
- Trailing 10d drawdown: **-2.80%**
- Trailing 4d return: **+0.12%**
- Trailing 4d drawdown: **-0.96%**

This was one of the main reasons for choosing Trial 1285. It behaved much better in the recent stress windows, which suggests it avoids overdeploying when the market is weak or noisy.

---

## Strategy logic in plain language

This bot is still a **mean-reversion grid bot**.

That means:
- it places buy orders below the anchor price
- it places sell orders above the anchor price
- it refreshes the ladder over time
- it does not use hard stop-loss trend logic

But unlike the older version, it now decides how much to deploy using shared portfolio context.

### Buy-side behavior
The bot asks:
- how far am I below my target sleeve value?
- how much shared cash is really available?
- how much reserve must stay untouched?
- how much inventory room remains?
- is the pair / portfolio stressed enough to unlock emergency cash?

Then it scales buy notional accordingly.

### Sell-side behavior
The bot asks:
- how much inventory is above the minimum floor?
- how over-target is the current sleeve?
- how much inventory is already committed in open sells?

Then it scales sell quantity accordingly.

---

## Shared-wallet behavior

This version is intended to be run as **three separate bot processes** from the same repo directory.

All three share:
- the same exchange wallet
- the same reserve logic
- the same portfolio peak state file

Shared file:
- `state/portfolio_state.json`

That file allows the bots to react to the **same portfolio drawdown state**, not three separate fake states.

This is important because the emergency reserve logic depends on real shared-wallet behavior.

---

## Files included for deployment

The deployment package for this version includes:
- `main.py`
- `src/config.py`
- `src/portfolio_runtime.py`
- `src/strategy.py`
- `.env.eth`
- `.env.sol`
- `.env.btc`
- `.env.example`
- `TRIAL_1285_DEPLOY_NOTES.md`

---

## Environment files

Use these weights:
- ETH bot: `GRID_TARGET_WEIGHT_PCT=0.50`
- SOL bot: `GRID_TARGET_WEIGHT_PCT=0.25`
- BTC bot: `GRID_TARGET_WEIGHT_PCT=0.25`

All three `.env` files should point to the same shared state file:
- `GRID_SHARED_STATE_FILE=state/portfolio_state.json`

---

## Launch commands

Run all three from the same repo root:

```bash
python main.py --env .env.eth
python main.py --env .env.sol
python main.py --env .env.btc
```

---

## Deployment notes

Before deploying:
- replace API keys in the `.env` files
- keep the correct Roostoo base URL for your environment
- ensure all three bots run from the same repo directory
- ensure the `state/` folder exists and is writable

---

## Final reasoning for the selection

Trial 1285 was selected because it gives the best practical balance of:
- improved return behavior
- lower drawdown
- better recent-window protection
- continued upside participation
- portfolio-aware reserve use

The base dynamic bot was too eager to deploy capital. Trial 1285 keeps the same overall grid philosophy, but adds discipline around **when** to spend cash and **how much** to commit.

That is the defining characteristic of this version.
