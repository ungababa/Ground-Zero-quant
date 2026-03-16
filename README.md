# Roostoo BTC/USD Grid Bot Baseline

Baseline maker-first BTC/USD grid bot for the Roostoo mock exchange competition.

## Why this baseline

The competition explicitly rewards both return and risk-adjusted metrics while discouraging HFT. A narrow, maker-first grid on BTC/USD fits that shape well:

- BTC/USD has the deepest liquidity in the mock exchange docs.
- Grid trading monetizes chop during the live windows.
- Limit-first execution keeps fees low.
- The control loop can run on a slow cadence without violating the spirit of the rules.

This implementation is intentionally original, but it borrows good engineering ideas from existing ecosystems instead of reinventing everything:

- `requests` for the live REST client
- `pandas` and `numpy` for analytics and backtesting
- `optuna` for offline parameter search
- optional `yfinance` for quick BTC-USD historical candles

Live trading code talks only to the Roostoo API.

## Competition details reflected here

From the public Luma page:

- Strategy build phase: Mar 16 – Mar 20
- Preliminary live trading: Mar 21 – Mar 31
- Final live trading: Apr 4 – Apr 14
- Code must be open-source and original
- Bots run 24/7 in the cloud
- Rate limits are enforced
- HFT is not supported; roughly one trade per minute is the intended operating style
- Judges care about portfolio return and composite risk-adjusted metrics like Sharpe, Sortino, and Calmar

## Strategy summary

This bot runs a symmetric BTC/USD grid around a reference price with a few practical protections:

- maker-only `LIMIT` orders
- fixed number of buy and sell levels around the mid price
- configurable spacing in percent
- configurable per-level notional budget
- max gross BTC exposure cap
- cooldown and re-centering threshold to avoid churn
- cancel-and-refresh only when the market moves materially
- optional trend filter to stand down in violent one-way moves

This is a baseline, not a magical alpha machine. It is designed to be stable, legible, and easy to tune.

## Project layout

```text
roostoo_grid_bot/
├── README.md
├── requirements.txt
├── .env.example
├── main.py
├── config.py
├── roostoo_client.py
├── strategy.py
├── backtest.py
├── metrics.py
└── optimizer.py
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with your Roostoo credentials before live trading.

## Live run

```bash
python3 main.py --pair SOL/USD --poll-seconds 60
```

## Backtest

```bash
python3 backtest.py --days 180
```

If `yfinance` is available, the script will download BTC-USD candles automatically. Otherwise pass a CSV with columns `timestamp,open,high,low,close`.

## Optimize

```bash
python3 optimizer.py --trials 50 --days 180
```

## Notes on Roostoo API behavior

The public docs and demo code imply:

- REST base URL: `https://mock-api.roostoo.com`
- `GET /v3/exchangeInfo` exposes price and amount precision plus minimum order value
- `GET /v3/ticker` provides `MaxBid`, `MinAsk`, and `LastPrice`
- signed endpoints use HMAC-SHA256 over sorted form/query parameters
- timestamp must be within roughly 60 seconds of server time
- `POST /v3/place_order` supports `LIMIT` and `MARKET`, but this baseline uses `LIMIT`

## Suggested first tuning pass

Start roughly here for BTC/USD:

- levels per side: `4`
- spacing: `0.35%`
- per-level notional: `$2,000`
- max position notional: `$12,000`
- refresh threshold: `0.30%`
- trend pause: `3.0%` absolute 24h change
- poll interval: `20s`

Then run offline optimization against recent BTC history and choose parameters that improve Calmar and drawdown, not just raw PnL.
