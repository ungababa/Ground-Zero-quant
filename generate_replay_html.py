import argparse
import json
from pathlib import Path

import pandas as pd

from metrics import summarize_equity_curve


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--output", default="artifacts/backtest_replay.html")
    parser.add_argument(
        "--pair", default="SOL/USD", help="Trading pair displayed in the report, e.g. SOL/USD or BTC/USD"
    )
    return parser.parse_args()


def _json_records(df: pd.DataFrame) -> str:
    return json.dumps(df.to_dict(orient="records"), separators=(",", ":"), default=str)


def main() -> None:
    args = parse_args()
    artifacts_dir = Path(args.artifacts_dir)
    output_path = Path(args.output)
    pair = args.pair
    coin = pair.split("/")[0]

    detail = pd.read_csv(artifacts_dir / "detailed_backtest_log.csv")
    trades = pd.read_csv(artifacts_dir / "trades.csv")
    equity = pd.read_csv(artifacts_dir / "equity_curve.csv")

    metrics = summarize_equity_curve(equity.rename(columns={"equity": "equity"}))
    periods = max(len(equity), 1)
    metrics["annualized_return"] = (1 + metrics["total_return"]) ** (365 / periods) - 1 if periods > 0 else 0.0
    metrics["trade_count"] = int(len(trades))

    for frame in (detail, trades, equity):
        if "timestamp" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Roostoo Grid Bot Backtest Replay</title>
  <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #121933;
      --panel-2: #0f1630;
      --text: #e8ecf7;
      --muted: #9fb0d0;
      --green: #34d399;
      --red: #f87171;
      --blue: #60a5fa;
      --yellow: #fbbf24;
      --border: #24304f;
    }}
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text); }}
    .wrap {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ margin: 0 0 16px; color: var(--muted); }}
    .metrics {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }}
    .controls {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }}
    button {{ background: var(--blue); color: white; border: 0; border-radius: 8px; padding: 10px 16px; cursor: pointer; font-weight: 600; }}
    button.secondary {{ background: #334155; }}
    input[type=range] {{ width: min(900px, 80vw); }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 2.4fr) minmax(340px, 1fr); gap: 16px; align-items: start; }}
    .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 14px; box-shadow: 0 8px 24px rgba(0,0,0,.22); }}
    .stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    .stat {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px; padding: 10px; }}
    .stat .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.04em; }}
    .stat .value {{ font-size: 18px; font-weight: 700; }}
    .lists {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 14px; }}
    .book {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px; padding: 10px; min-height: 260px; }}
    .book h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-family: ui-monospace, monospace; font-size: 12px; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,.04); }}
    .sell {{ color: var(--red); }}
    .buy {{ color: var(--green); }}
    .debug {{ margin-top: 14px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }}
    pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; color: var(--muted); font-size: 12px; }}
    #chart {{ height: 980px; }}
    .footer {{ margin-top: 12px; color: var(--muted); font-size: 12px; }}
    @media (max-width: 1200px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>Roostoo {pair} Grid Bot Backtest Replay</h1>
    <p>Generated from the actual backtest CSV artifacts. Use the slider or Play button to replay the simulation bar by bar.</p>

    <div class=\"metrics\" id=\"metricCards\"></div>

    <div class=\"controls\">
      <button id=\"playBtn\">Play</button>
      <button id=\"pauseBtn\" class=\"secondary\">Pause</button>
      <input id=\"stepSlider\" type=\"range\" min=\"0\" max=\"0\" value=\"0\" step=\"1\" />
      <span id=\"stepLabel\"></span>
    </div>

    <div class=\"grid\">
      <div class=\"panel\">
        <div id=\"chart\"></div>
      </div>
      <div class=\"panel\">
        <div class=\"stats\" id=\"stats\"></div>
        <div class=\"lists\">
          <div class=\"book\">
            <h3>Sell Limit Orders</h3>
            <div id=\"sellOrders\"></div>
          </div>
          <div class=\"book\">
            <h3>Buy Limit Orders</h3>
            <div id=\"buyOrders\"></div>
          </div>
        </div>
        <div class=\"debug\">
          <h3>Fills On Current Bar</h3>
          <pre id=\"fillsBox\"></pre>
        </div>
        <div class=\"debug\">
          <h3>Debug State</h3>
          <pre id=\"debugBox\"></pre>
        </div>
      </div>
    </div>
    <div class=\"footer\">Source files: detailed_backtest_log.csv, trades.csv, equity_curve.csv</div>
  </div>

  <script>
    const detail = {_json_records(detail)};
    const trades = {_json_records(trades)};
    const equity = {_json_records(equity)};
    const metrics = {json.dumps(metrics, separators=(",", ":"))};

    const slider = document.getElementById('stepSlider');
    const stepLabel = document.getElementById('stepLabel');
    const metricCards = document.getElementById('metricCards');
    const stats = document.getElementById('stats');
    const buyOrders = document.getElementById('buyOrders');
    const sellOrders = document.getElementById('sellOrders');
    const fillsBox = document.getElementById('fillsBox');
    const debugBox = document.getElementById('debugBox');
    const playBtn = document.getElementById('playBtn');
    const pauseBtn = document.getElementById('pauseBtn');

    slider.max = String(detail.length - 1);

    function fmt(num, digits = 2) {{
      const value = Number(num);
      if (!Number.isFinite(value)) return String(num);
      return value.toLocaleString(undefined, {{ minimumFractionDigits: digits, maximumFractionDigits: digits }});
    }}

    function parseJsonField(raw) {{
      if (!raw) return [];
      try {{ return JSON.parse(raw); }} catch {{ return []; }}
    }}

    const timestamps = detail.map(row => row.timestamp);
    const opens = detail.map(row => Number(row.open));
    const highs = detail.map(row => Number(row.high));
    const lows = detail.map(row => Number(row.low));
    const closes = detail.map(row => Number(row.close));
    const equityValues = equity.map(row => Number(row.equity));
    const unrealizedValues = detail.map(row => Number(row.unrealized_pnl_usd));
    const realizedValues = detail.map(row => Number(row.realized_pnl_usd));
    const buyTradeX = trades.filter(t => t.side === 'BUY').map(t => t.timestamp);
    const buyTradeY = trades.filter(t => t.side === 'BUY').map(t => Number(t.price));
    const sellTradeX = trades.filter(t => t.side === 'SELL').map(t => t.timestamp);
    const sellTradeY = trades.filter(t => t.side === 'SELL').map(t => Number(t.price));

    Plotly.newPlot('chart', [
      {{
        x: timestamps,
        open: opens,
        high: highs,
        low: lows,
        close: closes,
        type: 'candlestick',
        name: '{pair}',
        xaxis: 'x',
        yaxis: 'y',
      }},
      {{
        x: buyTradeX,
        y: buyTradeY,
        mode: 'markers',
        type: 'scatter',
        name: 'BUY fills',
        marker: {{ color: '#34d399', size: 7 }},
        xaxis: 'x',
        yaxis: 'y',
      }},
      {{
        x: sellTradeX,
        y: sellTradeY,
        mode: 'markers',
        type: 'scatter',
        name: 'SELL fills',
        marker: {{ color: '#f87171', size: 7 }},
        xaxis: 'x',
        yaxis: 'y',
      }},
      {{
        x: timestamps,
        y: equityValues,
        mode: 'lines',
        type: 'scatter',
        name: 'Equity',
        line: {{ color: '#fbbf24', width: 2 }},
        xaxis: 'x2',
        yaxis: 'y2',
      }},
      {{
        x: timestamps,
        y: unrealizedValues,
        mode: 'lines',
        type: 'scatter',
        name: 'Unrealized PnL',
        line: {{ color: '#34d399', width: 2 }},
        xaxis: 'x3',
        yaxis: 'y3',
      }},
      {{
        x: timestamps,
        y: realizedValues,
        mode: 'lines',
        type: 'scatter',
        name: 'Realized PnL',
        line: {{ color: '#f87171', width: 2 }},
        xaxis: 'x3',
        yaxis: 'y3',
      }},
      {{
        x: [timestamps[0]],
        y: [closes[0]],
        mode: 'markers',
        type: 'scatter',
        name: 'Current bar',
        marker: {{ color: '#60a5fa', size: 12, symbol: 'x' }},
        xaxis: 'x',
        yaxis: 'y',
        showlegend: false,
      }}
    ], {{
      paper_bgcolor: '#121933',
      plot_bgcolor: '#121933',
      font: {{ color: '#e8ecf7' }},
      xaxis: {{ domain: [0, 1], anchor: 'y', rangeslider: {{ visible: false }}, showticklabels: false, tickmode: 'auto', nticks: 8 }},
      yaxis: {{ domain: [0.56, 1.0], title: 'Price (USD)' }},
      xaxis2: {{ domain: [0, 1], anchor: 'y2', showticklabels: false, tickmode: 'auto', nticks: 8 }},
      yaxis2: {{ domain: [0.29, 0.49], title: 'Equity (USD)' }},
      xaxis3: {{ domain: [0, 1], anchor: 'y3', tickangle: -30, tickmode: 'auto', nticks: 10 }},
      yaxis3: {{ domain: [0.0, 0.22], title: 'PnL (USD)' }},
      margin: {{ l: 70, r: 20, t: 30, b: 90 }},
      legend: {{ orientation: 'h', y: -0.12 }},
      hovermode: 'x unified',
    }}, {{responsive: true}});

    function renderMetricCards() {{
      const values = [
        ['ROI', (metrics.total_return * 100).toFixed(2) + '%'],
        ['Annualized', (metrics.annualized_return * 100).toFixed(2) + '%'],
        ['Max DD', (metrics.max_drawdown * 100).toFixed(2) + '%'],
        ['Sharpe', Number(metrics.sharpe).toFixed(3)],
        ['Sortino', Number(metrics.sortino).toFixed(3)],
        ['Calmar', Number(metrics.calmar).toFixed(3)],
        ['Trades', String(metrics.trade_count)],
      ];
      metricCards.innerHTML = values.map(([label, value]) => `<div class=\"stat\"><div class=\"label\">${{label}}</div><div class=\"value\">${{value}}</div></div>`).join('');
    }}

    function renderStats(row, index) {{
      const values = [
        ['Timestamp', row.timestamp],
        ['Close', '$' + fmt(row.close)],
        ['Cash', '$' + fmt(row.cash_usd)],
        ['Position', fmt(row.current_position_coin, 6) + ' {coin}'],
        ['{coin} Value', '$' + fmt(row.coin_market_value_usd)],
        ['Position Notional', '$' + fmt(row.current_position_notional_usd)],
        ['Equity', '$' + fmt(row.equity_usd)],
        ['Realized PnL', '$' + fmt(row.realized_pnl_usd)],
        ['Unrealized PnL', '$' + fmt(row.unrealized_pnl_usd)],
        ['Anchor', '$' + fmt(row.anchor_price)],
        ['Fill Count', String(row.fill_count)],
      ];
      stats.innerHTML = values.map(([label, value]) => `<div class=\"stat\"><div class=\"label\">${{label}}</div><div class=\"value\">${{value}}</div></div>`).join('');
      stepLabel.textContent = `Bar ${{index + 1}} / ${{detail.length}}`;
    }}

    function renderOrders(rawOrders) {{
      const orders = parseJsonField(rawOrders);
      const buys = orders.filter(order => order.side === 'BUY').sort((a, b) => b.price - a.price);
      const sells = orders.filter(order => order.side === 'SELL').sort((a, b) => a.price - b.price);
      buyOrders.innerHTML = buys.map(order => `<div class=\"row buy\"><span>${{fmt(order.price)}}</span><span>${{fmt(order.quantity, 6)}}</span></div>`).join('');
      sellOrders.innerHTML = sells.map(order => `<div class=\"row sell\"><span>${{fmt(order.price)}}</span><span>${{fmt(order.quantity, 6)}}</span></div>`).join('');
    }}

    function renderDebug(row) {{
      fillsBox.textContent = JSON.stringify(parseJsonField(row.fills_json), null, 2);
      debugBox.textContent = JSON.stringify({{
        refresh_triggered: row.refresh_triggered,
        average_cost: Number(row.average_cost),
        spacing_pct: Number(row.debug_spacing_pct),
        levels_per_side: Number(row.debug_levels_per_side),
        max_position_notional_usd: Number(row.debug_max_position_notional_usd),
      }}, null, 2);
    }}

    function updateMarker(index) {{
      Plotly.restyle('chart', {{ x: [[timestamps[index]]], y: [[closes[index]]] }}, [6]);
    }}

    function render(index) {{
      const row = detail[index];
      renderStats(row, index);
      renderOrders(row.current_limit_orders_json);
      renderDebug(row);
      updateMarker(index);
      slider.value = String(index);
    }}

    slider.addEventListener('input', () => render(Number(slider.value)));

    let timer = null;
    playBtn.addEventListener('click', () => {{
      if (timer) return;
      timer = setInterval(() => {{
        const next = Number(slider.value) + 1;
        if (next >= detail.length) {{ clearInterval(timer); timer = null; return; }}
        render(next);
      }}, 250);
    }});

    pauseBtn.addEventListener('click', () => {{
      if (timer) {{ clearInterval(timer); timer = null; }}
    }});

    renderMetricCards();
    render(0);
  </script>
</body>
</html>
"""

    output_path.write_text(html, encoding="utf-8")
    print(json.dumps({"output_html": str(output_path)}, indent=2))


if __name__ == "__main__":
    main()
