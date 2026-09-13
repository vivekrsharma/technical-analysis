"""
Generate a Robinhood crypto portfolio dashboard as a local HTML file and open it.
Run with: op run --env-file=.env -- python3 -m robinhood.dashboard
"""
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import robin_stocks.robinhood as rh

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CT = ZoneInfo("America/Chicago")


def to_ct(dt: datetime) -> datetime:
    return dt.astimezone(CT)


def parse_dt(s):
    if not s:
        return None
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_dt(dt):
    return to_ct(dt).strftime("%Y-%m-%d %H:%M CT") if dt else "—"


def build_data():
    rh.login(
        username=os.environ["ROBINHOOD_USERNAME"],
        password=os.environ["ROBINHOOD_PASSWORD"],
        store_session=True,
    )

    profile   = rh.account.load_portfolio_profile() or {}
    raw_pos   = rh.crypto.get_crypto_positions() or []
    raw_orders = rh.orders.get_all_crypto_orders() or []

    # --- Positions ---
    positions = []
    symbols = []
    for p in raw_pos:
        qty = float(p.get("quantity", 0))
        if qty == 0:
            continue
        symbol = p["currency"]["code"]
        name   = p["currency"]["name"]
        symbols.append(symbol)

        tax_lots   = p.get("tax_lot_cost_bases", [{}])
        cost_bases = p.get("cost_bases", [{}])
        if tax_lots and tax_lots[0].get("clearing_book_cost_basis"):
            cost_basis = float(tax_lots[0]["clearing_book_cost_basis"])
        elif cost_bases:
            cost_basis = float(cost_bases[0].get("direct_cost_basis", 0))
        else:
            cost_basis = 0.0

        avg_buy = cost_basis / qty if qty else 0

        quote         = rh.crypto.get_crypto_quote(symbol) or {}
        current_price = float(quote.get("mark_price", 0))
        current_value = current_price * qty
        unreal_pnl    = current_value - cost_basis
        unreal_pct    = (unreal_pnl / cost_basis * 100) if cost_basis else 0

        qty_available = float(p.get("quantity_available", qty))
        qty_held      = float(p.get("quantity_held_for_sell", 0)) + float(p.get("quantity_held_for_buy", 0))

        positions.append({
            "symbol":        symbol,
            "name":          name,
            "quantity":      round(qty, 6),
            "qty_available": round(qty_available, 6),
            "qty_held":      round(qty_held, 6),
            "avg_buy":       round(avg_buy, 4),
            "cost_basis":    round(cost_basis, 2),
            "price":         round(current_price, 4),
            "value":         round(current_value, 2),
            "unreal_pnl":    round(unreal_pnl, 2),
            "unreal_pct":    round(unreal_pct, 2),
        })

    # --- Orders ---
    orders = []
    for o in raw_orders:
        sym   = o.get("currency_code", "")
        state = o.get("state", "")
        dt    = parse_dt(o.get("created_at"))
        orders.append({
            "order_id":   o.get("id", ""),
            "symbol":     sym,
            "side":       o.get("side", ""),
            "type":       o.get("type", ""),
            "state":      state,
            "quantity":   round(float(o.get("quantity", 0)), 6),
            "filled_qty": round(float(o.get("cumulative_quantity", 0)), 6),
            "price":      round(float(o.get("price") or 0), 4),
            "avg_price":  round(float(o.get("average_price") or 0), 4),
            "total":      round(float(o.get("rounded_executed_notional") or 0), 2),
            "date":       fmt_dt(dt),
            "date_ts":    to_ct(dt).strftime("%Y-%m-%d") if dt else "",
        })

    # --- Portfolio summary ---
    equity     = float(profile.get("equity", 0))
    prev_close = float(profile.get("equity_previous_close", equity))
    day_change = round(equity - prev_close, 2)
    day_pct    = round(day_change / prev_close * 100, 2) if prev_close else 0

    total_cost    = sum(p["cost_basis"] for p in positions)
    total_value   = sum(p["value"] for p in positions)
    total_unreal  = round(total_value - total_cost, 2)

    # Buys vs sells by symbol for realized P&L estimate
    realized_by_sym = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for o in orders:
        if o["state"] in ("filled", "partially_filled") and o["total"] > 0:
            realized_by_sym[o["symbol"]][o["side"]] += o["total"]
    realized = {
        sym: round(v["sell"] - v["buy"], 2)
        for sym, v in realized_by_sym.items()
    }

    # P&L over time (cumulative from filled orders)
    filled = sorted(
        [o for o in orders if o["state"] in ("filled", "partially_filled") and o["date_ts"]],
        key=lambda x: x["date_ts"],
    )
    cum, pnl_timeline = 0.0, []
    for o in filled:
        delta = o["total"] if o["side"] == "sell" else -o["total"]
        cum = round(cum + delta, 2)
        pnl_timeline.append({"date": o["date_ts"], "cum": cum, "symbol": o["symbol"]})

    return {
        "equity":        round(equity, 2),
        "day_change":    day_change,
        "day_pct":       day_pct,
        "total_cost":    round(total_cost, 2),
        "total_value":   round(total_value, 2),
        "total_unreal":  total_unreal,
        "positions":     positions,
        "orders":        orders,
        "realized":      realized,
        "pnl_timeline":  pnl_timeline,
        "n_orders":      len(orders),
        "generated_at":  datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"),
    }


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Robinhood Crypto Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;
    --text:#e2e8f0;--muted:#94a3b8;--green:#22c55e;
    --red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--purple:#a855f7;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:24px}
  h1{font-size:20px;font-weight:600;letter-spacing:.05em;margin-bottom:4px}
  .subtitle{color:var(--muted);margin-bottom:28px;font-size:12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:28px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px}
  .card-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
  .card-value{font-size:22px;font-weight:700}
  .card-sub{font-size:12px;margin-top:4px}
  .green{color:var(--green)} .red{color:var(--red)} .blue{color:var(--blue)}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px}
  .chart-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px}
  .chart-box h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}
  .chart-wrap{position:relative;height:220px}
  .section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px;overflow-x:auto}
  .section h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}
  table{width:100%;border-collapse:collapse}
  th{color:var(--muted);text-align:left;padding:6px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
  th:hover{color:var(--text)}
  th.sort-asc::after{content:' ↑';color:var(--blue)}
  th.sort-desc::after{content:' ↓';color:var(--blue)}
  td{padding:7px 12px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.03)}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  .muted{color:var(--muted)}
  .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
  .badge.buy{background:rgba(34,197,94,.15);color:var(--green)}
  .badge.sell{background:rgba(239,68,68,.15);color:var(--red)}
  .badge.filled{background:rgba(59,130,246,.15);color:var(--blue)}
  .badge.canceled{background:rgba(100,116,139,.15);color:var(--muted)}
  .badge.confirmed{background:rgba(245,158,11,.15);color:var(--yellow)}
  @media(max-width:768px){.charts{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>Robinhood Crypto</h1>
<p class="subtitle">Generated {{generated_at}}</p>

<div class="cards">
  <div class="card">
    <div class="card-label">Portfolio Equity</div>
    <div class="card-value blue">${{equity}}</div>
    <div class="card-sub {{day_class}}">{{day_change_str}} today ({{day_pct}}%)</div>
  </div>
  <div class="card">
    <div class="card-label">Crypto Value</div>
    <div class="card-value">${{total_value}}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Cost Basis</div>
    <div class="card-value">${{total_cost}}</div>
  </div>
  <div class="card">
    <div class="card-label">Unrealized P&amp;L</div>
    <div class="card-value {{unreal_class}}">{{unreal_str}}</div>
  </div>
  <div class="card">
    <div class="card-label">Holdings</div>
    <div class="card-value">{{n_positions}}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Orders</div>
    <div class="card-value">{{n_orders}}</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h2>Portfolio Allocation</h2>
    <div class="chart-wrap"><canvas id="allocChart"></canvas></div>
  </div>
  <div class="chart-box">
    <h2>Unrealized P&amp;L by Coin</h2>
    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
  </div>
</div>

<div class="charts">
  <div class="chart-box" style="grid-column:1/-1">
    <h2>Cumulative Cash Flow from Orders</h2>
    <div class="chart-wrap" style="height:200px"><canvas id="timelineChart"></canvas></div>
  </div>
</div>

<div class="section">
  <h2>Holdings</h2>
  <table id="holdingsTable">
    <thead><tr>
      <th>Coin</th><th>Name</th>
      <th class="num">Quantity</th><th class="num">Available</th>
      <th class="num">Avg Cost</th><th class="num">Price</th>
      <th class="num">Value</th><th class="num">Cost Basis</th>
      <th class="num">Unreal P&amp;L</th><th class="num">Unreal %</th>
    </tr></thead>
    <tbody id="holdingsBody"></tbody>
  </table>
</div>

<div class="section">
  <h2>Order History ({{n_orders}} orders)</h2>
  <table id="ordersTable">
    <thead><tr>
      <th>Date (CT)</th><th>Symbol</th><th>Side</th><th>Type</th><th>Status</th>
      <th class="num">Quantity</th><th class="num">Filled</th>
      <th class="num">Price</th><th class="num">Avg Price</th><th class="num">Total</th>
    </tr></thead>
    <tbody id="ordersBody"></tbody>
  </table>
</div>

<script>
const DATA = {{data_json}};
const GRID = 'rgba(255,255,255,0.07)', TICK = '#64748b';
const COLORS = ['#3b82f6','#22c55e','#f59e0b','#a855f7','#ef4444','#06b6d4'];

// --- Holdings table ---
DATA.positions.forEach(p => {
  const pnlClass = p.unreal_pnl >= 0 ? 'green' : 'red';
  const pnlStr   = (p.unreal_pnl >= 0 ? '+$' : '-$') + Math.abs(p.unreal_pnl).toFixed(2);
  const pctStr   = (p.unreal_pct >= 0 ? '+' : '') + p.unreal_pct.toFixed(2) + '%';
  document.getElementById('holdingsBody').innerHTML += `
    <tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="muted">${p.name}</td>
      <td class="num">${p.quantity.toLocaleString('en-US',{maximumFractionDigits:6})}</td>
      <td class="num muted">${p.qty_available.toLocaleString('en-US',{maximumFractionDigits:6})}</td>
      <td class="num">$${p.avg_buy.toLocaleString('en-US',{minimumFractionDigits:2})}</td>
      <td class="num">$${p.price.toLocaleString('en-US',{minimumFractionDigits:2})}</td>
      <td class="num"><strong>$${p.value.toLocaleString('en-US',{minimumFractionDigits:2})}</strong></td>
      <td class="num muted">$${p.cost_basis.toLocaleString('en-US',{minimumFractionDigits:2})}</td>
      <td class="num ${pnlClass}">${pnlStr}</td>
      <td class="num ${pnlClass}">${pctStr}</td>
    </tr>`;
});

// --- Orders table ---
DATA.orders.forEach(o => {
  const sideClass   = o.side === 'buy' ? 'buy' : 'sell';
  const stateClass  = o.state === 'filled' ? 'filled' : o.state === 'canceled' ? 'canceled' : 'confirmed';
  const avgStr = o.avg_price > 0 ? '$' + o.avg_price.toLocaleString('en-US',{minimumFractionDigits:2}) : '—';
  const totStr = o.total > 0    ? '$' + o.total.toFixed(2) : '—';
  document.getElementById('ordersBody').innerHTML += `
    <tr>
      <td class="muted">${o.date}</td>
      <td><strong>${o.symbol}</strong></td>
      <td><span class="badge ${sideClass}">${o.side.toUpperCase()}</span></td>
      <td class="muted">${o.type}</td>
      <td><span class="badge ${stateClass}">${o.state}</span></td>
      <td class="num">${o.quantity.toLocaleString('en-US',{maximumFractionDigits:6})}</td>
      <td class="num">${o.filled_qty.toLocaleString('en-US',{maximumFractionDigits:6})}</td>
      <td class="num muted">${o.price > 0 ? '$'+o.price.toLocaleString('en-US',{minimumFractionDigits:2}) : '—'}</td>
      <td class="num">${avgStr}</td>
      <td class="num">${totStr}</td>
    </tr>`;
});

// --- Allocation pie ---
new Chart(document.getElementById('allocChart'), {
  type: 'doughnut',
  data: {
    labels: DATA.positions.map(p => p.symbol),
    datasets: [{ data: DATA.positions.map(p => p.value), backgroundColor: COLORS, borderWidth: 2, borderColor: '#1a1d27' }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { position: 'right', labels: { color: TICK, font: { size: 11 }, padding: 12 } },
      tooltip: { callbacks: { label: ctx => ` ${ctx.label}: $${ctx.parsed.toLocaleString('en-US',{minimumFractionDigits:2})}` } }
    }
  }
});

// --- Unrealized P&L bar ---
const pnlColors = DATA.positions.map(p => p.unreal_pnl >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)');
new Chart(document.getElementById('pnlChart'), {
  type: 'bar',
  data: {
    labels: DATA.positions.map(p => p.symbol),
    datasets: [{ data: DATA.positions.map(p => p.unreal_pnl), backgroundColor: pnlColors, borderRadius: 5 }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ` $${ctx.parsed.y.toFixed(2)}` } } },
    scales: {
      x: { grid: { color: GRID }, ticks: { color: TICK } },
      y: { grid: { color: GRID }, ticks: { color: TICK, callback: v => '$'+v } }
    }
  }
});

// --- Timeline ---
const tl = DATA.pnl_timeline;
new Chart(document.getElementById('timelineChart'), {
  type: 'line',
  data: {
    labels: tl.map(p => p.date),
    datasets: [{
      data: tl.map(p => p.cum),
      borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)',
      fill: true, tension: 0.3, pointRadius: 0
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ' $'+ctx.parsed.y.toFixed(2) } } },
    scales: {
      x: { grid: { color: GRID }, ticks: { color: TICK, maxTicksLimit: 10 } },
      y: { grid: { color: GRID }, ticks: { color: TICK, callback: v => '$'+v } }
    }
  }
});

// --- Sortable tables ---
document.querySelectorAll('table').forEach(table => {
  let sortCol = -1, sortAsc = true;
  table.querySelectorAll('th').forEach((th, col) => {
    th.addEventListener('click', () => {
      if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
      table.querySelectorAll('th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      const tbody = table.querySelector('tbody');
      const rows  = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => {
        const at = a.cells[col]?.innerText.trim() ?? '';
        const bt = b.cells[col]?.innerText.trim() ?? '';
        const sv = t => /^\\d{4}-\\d{2}-\\d{2}/.test(t) ? new Date(t.replace(' ','T')).getTime() : (isNaN(parseFloat(t.replace(/[^0-9.\\-]/g,''))) ? t : parseFloat(t.replace(/[^0-9.\\-]/g,'')));
        const av = sv(at), bv = sv(bt);
        const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
        return sortAsc ? cmp : -cmp;
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
});
</script>
</body>
</html>"""


def main():
    print("Fetching Robinhood data...")
    data = build_data()

    day_class    = "green" if data["day_change"] >= 0 else "red"
    day_str      = ("+$" if data["day_change"] >= 0 else "-$") + f"{abs(data['day_change']):.2f}"
    unreal_class = "green" if data["total_unreal"] >= 0 else "red"
    unreal_str   = ("+$" if data["total_unreal"] >= 0 else "-$") + f"{abs(data['total_unreal']):.2f}"

    html = HTML.replace("{{data_json}}",     json.dumps(data))
    html = html.replace("{{generated_at}}", data["generated_at"])
    html = html.replace("{{equity}}",        f"{data['equity']:,.2f}")
    html = html.replace("{{day_class}}",     day_class)
    html = html.replace("{{day_change_str}}", day_str)
    html = html.replace("{{day_pct}}",       f"{data['day_pct']:+.2f}")
    html = html.replace("{{total_value}}",   f"{data['total_value']:,.2f}")
    html = html.replace("{{total_cost}}",    f"{data['total_cost']:,.2f}")
    html = html.replace("{{unreal_class}}",  unreal_class)
    html = html.replace("{{unreal_str}}",    unreal_str)
    html = html.replace("{{n_positions}}",   str(len(data["positions"])))
    html = html.replace("{{n_orders}}",      str(data["n_orders"]))

    out = os.path.join(tempfile.gettempdir(), "robinhood_dashboard.html")
    with open(out, "w") as f:
        f.write(html)

    print(f"Saved to {out}")
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
