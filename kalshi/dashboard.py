"""
Generate a Kalshi performance dashboard as a local HTML file and open it.
Run with: op run --env-file=.env -- python3 -m kalshi.dashboard
"""
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from kalshi import KalshiClient


def parse_dt(s):
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def paginate(session, url, key, limit=100):
    results, cursor = [], None
    while True:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = session.get(url, params=params).json()
        batch = data.get(key, [])
        results.extend(batch)
        cursor = data.get("cursor")
        if not cursor or len(batch) < limit:
            break
    return results


def build_data(c):
    base = "https://api.elections.kalshi.com/trade-api/v2"
    settlements = paginate(c.session, f"{base}/portfolio/settlements", "settlements")
    fills_raw   = paginate(c.session, f"{base}/portfolio/fills", "fills")
    positions   = c.get_positions()
    balance     = c.get_balance()

    fills_by_ticker = defaultdict(list)
    for f in fills_raw:
        fills_by_ticker[f["market_ticker"]].append(f)

    # Fetch market data (volume + open interest) for every settlement ticker in parallel
    unique_tickers = list({s["ticker"] for s in settlements})
    def fetch_market(ticker):
        r = c.session.get(f"{base}/markets/{ticker}")
        m = r.json().get("market", {}) if r.ok else {}
        return ticker, {
            "title":    m.get("title", ticker),
            "volume":   round(float(m.get("volume_fp", 0)), 2),
            "oi":       round(float(m.get("open_interest_fp", 0)), 2),
        }

    market_data = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_market, t): t for t in unique_tickers}
        for fut in as_completed(futures):
            ticker, info = fut.result()
            market_data[ticker] = info

    rows = []
    for s in sorted(settlements, key=lambda x: x["settled_time"], reverse=True):
        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty  = float(s.get("no_count_fp", 0))
        result  = s.get("market_result", "")
        revenue = s.get("revenue", 0) / 100

        side = "YES" if yes_qty > 0 else "NO"
        qty  = yes_qty if yes_qty > 0 else no_qty
        cost = float(s.get("yes_total_cost_dollars" if yes_qty > 0 else "no_total_cost_dollars", 0))
        won  = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        pnl  = revenue - cost if won else -cost

        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if ticker_fills:
            earliest     = min(ticker_fills, key=lambda f: f["created_time"])
            placed_dt    = parse_dt(earliest["created_time"])
            placed_str   = placed_dt.strftime("%H:%M")
            yes_contracts = round(sum(float(f["count_fp"]) for f in ticker_fills if f.get("side") == "yes"), 2)
            no_contracts  = round(sum(float(f["count_fp"]) for f in ticker_fills if f.get("side") == "no"),  2)
        else:
            placed_str    = "—"
            placed_dt     = parse_dt(s["settled_time"])
            yes_contracts = round(yes_qty, 2)
            no_contracts  = round(no_qty,  2)

        total_contracts = round(yes_contracts + no_contracts, 2)

        mkt = market_data.get(s["ticker"], {})
        rows.append({
            "ticker":          s["ticker"],
            "mkt_title":       mkt.get("title", s["ticker"]),
            "mkt_volume":      mkt.get("volume", 0),
            "mkt_oi":          mkt.get("oi", 0),
            "settled":         parse_dt(s["settled_time"]).strftime("%Y-%m-%d %H:%M"),
            "placed":          placed_str,
            "placed_hour":     placed_dt.hour,
            "placed_date":     placed_dt.strftime("%Y-%m-%d"),
            "result":          "WIN" if won else "LOSS",
            "side":            side,
            "yes_contracts":   yes_contracts,
            "no_contracts":    no_contracts,
            "total_contracts": total_contracts,
            "placed_usd":      round(cost, 2),
            "payout":          round(revenue, 2),
            "pnl":             round(pnl, 2),
            "won":             won,
        })

    # Cumulative P&L over time
    cum_pnl = 0.0
    for r in reversed(rows):
        cum_pnl += r["pnl"]
        r["cum_pnl"] = round(cum_pnl, 2)

    # Hour breakdown
    hour_stats = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for r in rows:
        h = r["placed_hour"]
        hour_stats[h]["bets"] += 1
        hour_stats[h]["wins"] += int(r["won"])
        hour_stats[h]["pnl"]  += r["pnl"]

    # Market type breakdown
    market_stats = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for r in rows:
        mtype = r["ticker"].split("-")[0]
        market_stats[mtype]["bets"] += 1
        market_stats[mtype]["wins"] += int(r["won"])
        market_stats[mtype]["pnl"]  += r["pnl"]

    total_pnl       = sum(r["pnl"] for r in rows)
    total_cost      = sum(r["placed_usd"] for r in rows)
    total_contracts = round(sum(r["total_contracts"] for r in rows), 2)
    wins            = sum(1 for r in rows if r["won"])

    pos_list = [
        {
            "ticker":   p.ticker,
            "position": p.position,
            "cost":     round(p.total_cost / 100, 2),
            "unreal":   round(p.unrealized_pnl / 100, 2),
            "real":     round(p.realized_pnl / 100, 2),
        }
        for p in positions
    ]

    return {
        "balance":      round(balance / 100, 2),
        "total_pnl":    round(total_pnl, 2),
        "total_cost":   round(total_cost, 2),
        "wins":         wins,
        "losses":       len(rows) - wins,
        "total":        len(rows),
        "win_rate":        round(wins / len(rows) * 100, 1) if rows else 0,
        "total_contracts": total_contracts,
        "rows":            rows,
        "hour_stats":   {str(h): v for h, v in sorted(hour_stats.items())},
        "market_stats": {k: v for k, v in sorted(market_stats.items(), key=lambda x: -x[1]["bets"])},
        "positions":    pos_list,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #94a3b8; --green: #22c55e;
    --red: #ef4444; --blue: #3b82f6; --yellow: #f59e0b;
    --purple: #a855f7;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; padding: 24px; }
  h1 { font-size: 20px; font-weight: 600; letter-spacing: 0.05em; margin-bottom: 6px; }
  .subtitle { color: var(--muted); margin-bottom: 28px; font-size: 12px; }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 28px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
  .card-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px; }
  .card-value { font-size: 24px; font-weight: 700; }
  .card-value.green { color: var(--green); }
  .card-value.red   { color: var(--red); }
  .card-value.blue  { color: var(--blue); }

  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 28px; }
  .chart-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .chart-box h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 220px; }

  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 20px; overflow-x: auto; }
  .section h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 16px; }

  table { width: 100%; border-collapse: collapse; }
  th { color: var(--muted); text-align: left; padding: 6px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 7px 12px; border-bottom: 1px solid rgba(255,255,255,0.04); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.03); }
  .win  { color: var(--green); font-weight: 600; }
  .loss { color: var(--red); }
  .num  { text-align: right; font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.win  { background: rgba(34,197,94,0.15); color: var(--green); }
  .badge.loss { background: rgba(239,68,68,0.15);  color: var(--red); }
  .tag  { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; background: rgba(59,130,246,0.15); color: var(--blue); }
  .tag.no { background: rgba(168,85,247,0.15); color: var(--purple); }

  @media (max-width: 768px) { .charts { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<h1>Kalshi Dashboard</h1>
<p class="subtitle">Generated {{generated_at}}</p>

<div class="cards">
  <div class="card">
    <div class="card-label">Balance</div>
    <div class="card-value blue">${{balance}}</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&amp;L</div>
    <div class="card-value {{pnl_class}}">{{total_pnl_str}}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Wagered</div>
    <div class="card-value">${{total_cost}}</div>
  </div>
  <div class="card">
    <div class="card-label">Record</div>
    <div class="card-value">{{wins}}W / {{losses}}L</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value {{wr_class}}">{{win_rate}}%</div>
  </div>
  <div class="card">
    <div class="card-label">Bets Placed</div>
    <div class="card-value">{{total}}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Contracts</div>
    <div class="card-value blue">{{total_contracts}}</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h2>Cumulative P&amp;L</h2>
    <div class="chart-wrap"><canvas id="cumulChart"></canvas></div>
  </div>
  <div class="chart-box">
    <h2>P&amp;L by Hour (UTC)</h2>
    <div class="chart-wrap"><canvas id="hourChart"></canvas></div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h2>Win Rate by Hour (UTC)</h2>
    <div class="chart-wrap"><canvas id="winRateChart"></canvas></div>
  </div>
  <div class="chart-box">
    <h2>Bets by Market Type</h2>
    <div class="chart-wrap"><canvas id="marketChart"></canvas></div>
  </div>
</div>

<div class="section">
  <h2>Settled Bets</h2>
  <table>
    <thead>
      <tr>
        <th>Settled</th><th>Placed</th><th>Ticker</th>
        <th>Market</th><th>Result</th><th>Side</th>
        <th class="num">YES Cts</th><th class="num">NO Cts</th><th class="num">Total Cts</th>
        <th class="num">Mkt Volume</th><th class="num">Open Interest</th>
        <th class="num">$ Placed</th><th class="num">Payout</th><th class="num">P&amp;L</th>
      </tr>
    </thead>
    <tbody id="betsBody"></tbody>
  </table>
</div>

{{positions_section}}

<script>
const DATA = {{data_json}};

// --- Bets table ---
const tbody = document.getElementById('betsBody');
DATA.rows.forEach(r => {
  const pnlClass = r.pnl >= 0 ? 'win' : 'loss';
  const pnlStr   = (r.pnl >= 0 ? '+' : '') + '$' + r.pnl.toFixed(2);
  tbody.innerHTML += `
    <tr>
      <td class="muted">${r.settled}</td>
      <td class="muted">${r.placed}</td>
      <td>${r.ticker}</td>
      <td class="muted" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis" title="${r.mkt_title}">${r.mkt_title}</td>
      <td><span class="badge ${r.won ? 'win' : 'loss'}">${r.result}</span></td>
      <td><span class="tag ${r.side === 'NO' ? 'no' : ''}">${r.side}</span></td>
      <td class="num">${r.yes_contracts > 0 ? r.yes_contracts.toFixed(2) : '—'}</td>
      <td class="num">${r.no_contracts  > 0 ? r.no_contracts.toFixed(2)  : '—'}</td>
      <td class="num" style="font-weight:600">${r.total_contracts.toFixed(2)}</td>
      <td class="num muted">${r.mkt_volume.toLocaleString()}</td>
      <td class="num muted">${r.mkt_oi.toLocaleString()}</td>
      <td class="num">$${r.placed_usd.toFixed(2)}</td>
      <td class="num">$${r.payout.toFixed(2)}</td>
      <td class="num ${pnlClass}">${pnlStr}</td>
    </tr>`;
});

const GRID_COLOR  = 'rgba(255,255,255,0.07)';
const TICK_COLOR  = '#64748b';
const chartDefaults = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR } },
    y: { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR } },
  },
};

// --- Cumulative P&L ---
const cumLabels = DATA.rows.map(r => r.settled.slice(0, 10)).reverse();
const cumValues = DATA.rows.map(r => r.cum_pnl).reverse();
new Chart(document.getElementById('cumulChart'), {
  type: 'line',
  data: {
    labels: cumLabels,
    datasets: [{
      data: cumValues,
      borderColor: cumValues[cumValues.length-1] >= 0 ? '#22c55e' : '#ef4444',
      backgroundColor: cumValues[cumValues.length-1] >= 0
        ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)',
      fill: true, tension: 0.3, pointRadius: 3, pointHoverRadius: 5,
    }]
  },
  options: {
    ...chartDefaults,
    plugins: { ...chartDefaults.plugins, tooltip: {
      callbacks: { label: ctx => ' $' + ctx.parsed.y.toFixed(2) }
    }},
    scales: { ...chartDefaults.scales, y: {
      ...chartDefaults.scales.y,
      ticks: { ...chartDefaults.scales.y.ticks, callback: v => '$' + v }
    }}
  }
});

// --- P&L by hour ---
const hours    = Object.keys(DATA.hour_stats).map(Number);
const hourPnls = hours.map(h => DATA.hour_stats[h].pnl);
new Chart(document.getElementById('hourChart'), {
  type: 'bar',
  data: {
    labels: hours.map(h => `${String(h).padStart(2,'0')}:00`),
    datasets: [{
      data: hourPnls,
      backgroundColor: hourPnls.map(v => v >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)'),
      borderRadius: 4,
    }]
  },
  options: {
    ...chartDefaults,
    scales: { ...chartDefaults.scales, y: {
      ...chartDefaults.scales.y,
      ticks: { ...chartDefaults.scales.y.ticks, callback: v => '$' + v }
    }}
  }
});

// --- Win rate by hour ---
const hourWinRates = hours.map(h => {
  const st = DATA.hour_stats[h];
  return st.bets > 0 ? Math.round(st.wins / st.bets * 100) : 0;
});
new Chart(document.getElementById('winRateChart'), {
  type: 'bar',
  data: {
    labels: hours.map(h => `${String(h).padStart(2,'0')}:00`),
    datasets: [{
      data: hourWinRates,
      backgroundColor: hourWinRates.map(v => v >= 50 ? 'rgba(59,130,246,0.7)' : 'rgba(100,116,139,0.5)'),
      borderRadius: 4,
    }]
  },
  options: {
    ...chartDefaults,
    scales: { ...chartDefaults.scales, y: {
      ...chartDefaults.scales.y, min: 0, max: 100,
      ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + '%' }
    }}
  }
});

// --- Bets by market type ---
const mKeys = Object.keys(DATA.market_stats);
const mBets = mKeys.map(k => DATA.market_stats[k].bets);
const mPnls = mKeys.map(k => DATA.market_stats[k].pnl);
new Chart(document.getElementById('marketChart'), {
  type: 'bar',
  data: {
    labels: mKeys,
    datasets: [
      { label: 'Bets', data: mBets, backgroundColor: 'rgba(59,130,246,0.7)', borderRadius: 4, yAxisID: 'y' },
      { label: 'P&L', data: mPnls, backgroundColor: mPnls.map(v => v >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)'), borderRadius: 4, yAxisID: 'y2' },
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: true, labels: { color: TICK_COLOR, font: { size: 11 } } } },
    scales: {
      x:  { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR } },
      y:  { grid: { color: GRID_COLOR }, ticks: { color: TICK_COLOR }, position: 'left' },
      y2: { grid: { display: false },    ticks: { color: TICK_COLOR, callback: v => '$'+v }, position: 'right' },
    }
  }
});
</script>
</body>
</html>"""


def build_positions_section(positions):
    if not positions:
        return ""
    rows = ""
    for p in positions:
        upnl_cls = "win" if p["unreal"] >= 0 else "loss"
        rpnl_cls = "win" if p["real"]   >= 0 else "loss"
        rows += (
            f"<tr>"
            f"<td>{p['ticker']}</td>"
            f"<td class='num'>{p['position']:+d}</td>"
            f"<td class='num'>${p['cost']:.2f}</td>"
            f"<td class='num {upnl_cls}'>{'+' if p['unreal'] >= 0 else ''}${p['unreal']:.2f}</td>"
            f"<td class='num {rpnl_cls}'>{'+' if p['real'] >= 0 else ''}${p['real']:.2f}</td>"
            f"</tr>"
        )
    return f"""
<div class="section">
  <h2>Open Positions ({len(positions)})</h2>
  <table>
    <thead><tr><th>Ticker</th><th class="num">Position</th><th class="num">Cost</th>
    <th class="num">Unrealized P&L</th><th class="num">Realized P&L</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def main():
    c = KalshiClient()
    print("Fetching data...")
    data = build_data(c)

    pnl_class = "green" if data["total_pnl"] >= 0 else "red"
    wr_class  = "green" if data["win_rate"] >= 50 else "red"
    pnl_str   = ("+$" if data["total_pnl"] >= 0 else "-$") + f"{abs(data['total_pnl']):.2f}"

    html = HTML_TEMPLATE.replace("{{data_json}}", json.dumps(data))
    html = html.replace("{{generated_at}}", datetime.now().strftime("%Y-%m-%d %H:%M"))
    html = html.replace("{{balance}}",      str(data["balance"]))
    html = html.replace("{{pnl_class}}",    pnl_class)
    html = html.replace("{{total_pnl_str}}", pnl_str)
    html = html.replace("{{total_cost}}",   str(data["total_cost"]))
    html = html.replace("{{wins}}",         str(data["wins"]))
    html = html.replace("{{losses}}",       str(data["losses"]))
    html = html.replace("{{win_rate}}",     str(data["win_rate"]))
    html = html.replace("{{wr_class}}",     wr_class)
    html = html.replace("{{total}}",            str(data["total"]))
    html = html.replace("{{total_contracts}}", str(data["total_contracts"]))
    html = html.replace("{{positions_section}}", build_positions_section(data["positions"]))

    out = os.path.join(tempfile.gettempdir(), "kalshi_dashboard.html")
    with open(out, "w") as f:
        f.write(html)

    print(f"Dashboard saved to {out}")
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
