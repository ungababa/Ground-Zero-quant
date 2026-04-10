# Ground Zero Quant Bot 📈

A Python-based **Grid Trading Bot** for cryptocurrency markets, built to capitalize on price volatility within defined corridors. Developed as part of a university-level quantitative trading competition, placing **5th in portfolio return** against teams from top-tier universities across Hong Kong and Singapore.

---

## 🧠 Strategy Overview

Grid trading is a systematic strategy that places buy and sell orders at regular price intervals above and below a set price — creating a "grid" of orders. As the market oscillates within the corridor, the bot automatically executes trades to capture profit from volatility, without needing to predict market direction.

```
Price
  ▲
  │  ── Sell ──────────────────
  │  ── Sell ──────────────────
  │  ── Base Price ────────────
  │  ── Buy ───────────────────
  │  ── Buy ───────────────────
  └──────────────────────────► Time
```

---

## ⚙️ Features

- **Automated grid execution** — places and manages buy/sell orders within configurable price corridors
- **Volatility-optimised** — designed to profit from sideways and ranging market conditions
- **Configurable parameters** — grid spacing, upper/lower bounds, and position sizing are all adjustable
- **Real-time market monitoring** — continuously tracks price and triggers orders on threshold crossings

---

## 🛠️ Tech Stack

- **Language:** Python
- **Exchange API:** (e.g. Binance / Bybit — update as appropriate)
- **Libraries:** `ccxt`, `pandas`, `numpy`

---

## 🚀 Getting Started

### Prerequisites

```bash
pip install -r requirements.txt
```

### Configuration

Edit `config.py` (or `.env`) to set your parameters:

```python
SYMBOL        = "BTC/USDT"
UPPER_PRICE   = 70000
LOWER_PRICE   = 60000
GRID_LEVELS   = 10
INVESTMENT    = 1000   # USDT
API_KEY       = "your_api_key"
API_SECRET    = "your_api_secret"
```

### Run

```bash
python main.py
```

---
