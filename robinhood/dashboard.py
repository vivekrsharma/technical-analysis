"""
Generate a Robinhood crypto performance dashboard as a local HTML file.
Run with: op run --env-file=.env -- python3 -m robinhood.dashboard
"""
import json
import os
import subprocess
import tempfile
from zoneinfo import ZoneInfo
from robinhood import RobinhoodClient

CT = ZoneInfo("America/Chicago")


def fmt_dt(dt):
    if not dt:
        return "—"
    return dt.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")


def build_data(c: RobinhoodClient) -> dict:
    positions = c.get_positions()
    orders    = c.get_orders(state="filled")
    pnl_map   = c.get_pnl_summary()

    pos_rows = [
        {
            "symbol":         p.symbol,
            "name":           p.name,
            "quantity":       p.quantity,
            "avg_buy":        p.average_buy_price,
            "cost_basis":     p.cost_basis,
            "current_price":  p.current_price,
            "current_value":  p.current_value,
            "unrealized_pnl": p.unrealized_pnl,
            "unrealized_pct": p.unrealized_pnl_pct,
        }
        for p in positions
    ]

    order_rows = [
        {
            "date":      fmt_dt(o.created_at),
            "symbol":    o.symbol,
            "side":      o.side,
            "type":      o.order_type,
            "quantity":  o.filled_quantity,
            "avg_price": o.average_price or 0,
            "total":     o.total_notional,
        }
        for o in orders
    ]

    pnl_rows = [
        {
            "symbol":         sym,
            "realized_pnl":   d.get("realized_pnl", 0),
            "unrealized_pnl": d.get("unrealized_pnl", 0),
            "total_bought":   d.get("total_bought", 0),
            "total_sold":     d.get("total_sold", 0),
            "current_value":  d.get("current_value", 0),
        }
        for sym, d in sorted(pnl_map.items())
    ]

    total_value      = round(sum(p.current_value  for p in positions), 2)
    total_cost       = round(sum(p.cost_basis      for p in positions), 2)
    total_unrealized = round(sum(p.unrealized_pnl  for p in positions), 2)
    total_realized   = round(sum(d.get("realized_pnl", 0) for d in pnl_map.values()), 2)
    total_invested   = round(sum(d.get("total_bought",  0) for d in pnl_map.values()), 2)

    buys = sorted([o for o in orders if o.side == "buy" and o.created_at], key=lambda o: o.created_at)
    cum, cum_data = 0.0, []
    for o in buys:
        cum += o.total_notional
        cum_data.append({"date": o.created_at.astimezone(CT).strftime("%Y-%m-%d"), "total": round(cum, 2)})

    alloc = [{"symbol": p.symbol, "value": p.current_value} for p in positions if p.current_value > 0]

    import datetime
    return {
        "total_value":      total_value,
        "total_cost":       total_cost,
        "total_unrealized": total_unrealized,
        "total_realized":   total_realized,
        "total_invested":   total_invested,
        "num_assets":       len(positions),
        "positions":        pos_rows,
        "orders":           order_rows,
        "pnl":              pnl_rows,
        "cum_invest":       cum_data,
        "alloc":            alloc,
        "generated_at":     datetime.datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"),
    }


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Robinhood Crypto Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
    --muted:#94a3b8;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--purple:#a855f7;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:24px;}
  h1{font-size:20px;font-weight:600;letter-spacing:.05em;margin-bottom:4px;}
  .subtitle{color:var(--muted);margin-bottom:28px;font-size:12px;}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:28px;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px;}
  .card-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;}
  .card-value{font-size:24px;font-weight:700;}
  .green{color:var(--green);}.red{color:var(--red);}.blue{color:var(--blue);}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px;}
  .chart-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;}
  .chart-box h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;}
  .chart-wrap{position:relative;height:220px;}
  .section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px;overflow-x:auto;}
  .section h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;}
  table{width:100%;border-collapse:collapse;}
  th{color:var(--muted);text-align:left;padding:6px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none;}
  th:hover{color:var(--text);}
  th.sort-asc::after{content:' ↑';color:var(--blue);}
  th.sort-desc::after{content:' ↓';color:var(--blue);}
  td{padding:7px 12px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:rgba(255,255,255,.03);}
  .num{text-align:right;font-variant-numeric:tabular-nums;}
  .muted{color:var(--muted);}
  .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600;}
  .badge.buy{background:rgba(34,197,94,.15);color:var(--green);}
  .badge.sell{background:rgba(239,68,68,.15);color:var(--red);}
  .tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;background:rgba(59,130,246,.15);color:var(--blue);}
  @media(max-width:768px){.charts{grid-template-columns:1fr;}}
</style>
</head>
<body>
<h1>Robinhood Crypto</h1>
<p class="subtitle">Generated {{generated_at}}</p>

<div class="cards">
  <div class="card"><div class="card-label">Portfolio Value</div><div class="card-value blue">${{total_value}}</div></div>
  <div class="card"><div class="card-label">Unrealized P&amp;L</div><div class="card-value {{upnl_class}}">{{upnl_str}}</div></div>
  <div class="card"><div class="card-label">Realized P&amp;L</div><div class="card-value {{rpnl_class}}">{{rpnl_str}}</div></div>
  <div class="card"><div class="card-label">Total Invested</div><div class="card-value">${{total_invested}}</div></div>
  <div class="card"><div class="card-label">Cost Basis</div><div class="card-value">${{total_cost}}</div></div>
  <div class="card"><div class="card-label">Assets</div><div class="card-value">{{num_assets}}</div></div>
</div>

<div class="charts">
  <div class="chart-box"><h2>Cumulative Investment Over Time</h2><div class="chart-wrap"><canvas id="cumChart"></canvas></div></div>
  <div class="chart-box"><h2>Portfolio Allocation</h2><div class="chart-wrap"><canvas id="allocChart"></canvas></div></div>
</div>
<div class="charts">
  <div class="chart-box"><h2>Realized P&amp;L by Asset</h2><div class="chart-wrap"><canvas id="rpnlChart"></canvas></div></div>
  <div class="chart-box"><h2>Unrealized P&amp;L by Asset</h2><div class="chart-wrap"><canvas id="upnlChart"></canvas></div></div>
</div>

<div class="section">
  <h2>Open Positions</h2>
  <table><thead><tr>
    <th>Symbol</th><th>Name</th>
    <th class="num">Qty</th><th class="num">Avg Buy</th><th class="num">Cost Basis</th>
    <th class="num">Price</th><th class="num">Value</th>
    <th class="num">Unrealized P&amp;L</th><th class="num">%</th>
  </tr></thead><tbody id="posBody"></tbody></table>
</div>

<div class="section">
  <h2>P&amp;L Summary</h2>
  <table><thead><tr>
    <th>Symbol</th>
    <th class="num">Total Bought</th><th class="num">Total Sold</th>
    <th class="num">Realized P&amp;L</th><th class="num">Unrealized P&amp;L</th><th class="num">Current Value</th>
  </tr></thead><tbody id="pnlBody"></tbody></table>
</div>

<div class="section">
  <h2>Order History ({{order_count}} filled orders)</h2>
  <table><thead><tr>
    <th>Date (CT)</th><th>Symbol</th><th>Side</th><th>Type</th>
    <th class="num">Qty</th><th class="num">Avg Price</th><th class="num">Total</th>
  </tr></thead><tbody id="ordBody"></tbody></table>
</div>

<script>
const D = {{data_json}};
const GC='rgba(255,255,255,0.07)',TC='#64748b';
const base={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
  scales:{x:{grid:{color:GC},ticks:{color:TC}},y:{grid:{color:GC},ticks:{color:TC}}}};

D.positions.forEach(p=>{
  const cls=p.unrealized_pnl>=0?'green':'red';
  const pnl=(p.unrealized_pnl>=0?'+$':'-$')+Math.abs(p.unrealized_pnl).toFixed(2);
  const pct=(p.unrealized_pct>=0?'+':'')+p.unrealized_pct.toFixed(1)+'%';
  document.getElementById('posBody').innerHTML+=`<tr>
    <td><strong>${p.symbol}</strong></td><td class="muted">${p.name}</td>
    <td class="num">${p.quantity.toFixed(6)}</td><td class="num">$${p.avg_buy.toFixed(2)}</td>
    <td class="num">$${p.cost_basis.toFixed(2)}</td><td class="num">$${p.current_price.toFixed(2)}</td>
    <td class="num">$${p.current_value.toFixed(2)}</td>
    <td class="num ${cls}">${pnl}</td><td class="num ${cls}">${pct}</td></tr>`;
});

D.pnl.forEach(r=>{
  const rcls=r.realized_pnl>=0?'green':'red';
  const ucls=r.unrealized_pnl>=0?'green':'red';
  const rp=(r.realized_pnl>=0?'+$':'-$')+Math.abs(r.realized_pnl).toFixed(2);
  const up=(r.unrealized_pnl>=0?'+$':'-$')+Math.abs(r.unrealized_pnl).toFixed(2);
  document.getElementById('pnlBody').innerHTML+=`<tr>
    <td><strong>${r.symbol}</strong></td>
    <td class="num">$${r.total_bought.toFixed(2)}</td><td class="num">$${r.total_sold.toFixed(2)}</td>
    <td class="num ${rcls}">${rp}</td><td class="num ${ucls}">${up}</td>
    <td class="num">${r.current_value>0?'$'+r.current_value.toFixed(2):'—'}</td></tr>`;
});

D.orders.forEach(o=>{
  document.getElementById('ordBody').innerHTML+=`<tr>
    <td class="muted">${o.date}</td><td><strong>${o.symbol}</strong></td>
    <td><span class="badge ${o.side}">${o.side.toUpperCase()}</span></td>
    <td><span class="tag">${o.type}</span></td>
    <td class="num">${o.quantity.toFixed(6)}</td>
    <td class="num">$${o.avg_price.toFixed(2)}</td>
    <td class="num">$${o.total.toFixed(2)}</td></tr>`;
});

new Chart(document.getElementById('cumChart'),{type:'line',
  data:{labels:D.cum_invest.map(d=>d.date),datasets:[{data:D.cum_invest.map(d=>d.total),
    borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,0.08)',fill:true,tension:0.3,pointRadius:2}]},
  options:{...base,plugins:{...base.plugins,tooltip:{callbacks:{label:c=>' $'+c.parsed.y.toFixed(2)}}},
    scales:{...base.scales,y:{...base.scales.y,ticks:{...base.scales.y.ticks,callback:v=>'$'+v}}}}});

const COLORS=['#3b82f6','#22c55e','#f59e0b','#a855f7','#f97316','#06b6d4'];
new Chart(document.getElementById('allocChart'),{type:'doughnut',
  data:{labels:D.alloc.map(a=>a.symbol),datasets:[{data:D.alloc.map(a=>a.value),
    backgroundColor:COLORS,borderWidth:2,borderColor:'#1a1d27'}]},
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:true,position:'right',labels:{color:TC,font:{size:12}}},
      tooltip:{callbacks:{label:c=>` $${c.parsed.toFixed(2)}`}}}}});

const pnlSyms=D.pnl.map(r=>r.symbol);
const rv=D.pnl.map(r=>r.realized_pnl);
new Chart(document.getElementById('rpnlChart'),{type:'bar',
  data:{labels:pnlSyms,datasets:[{data:rv,
    backgroundColor:rv.map(v=>v>=0?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)'),borderRadius:4}]},
  options:{...base,scales:{...base.scales,y:{...base.scales.y,ticks:{...base.scales.y.ticks,callback:v=>'$'+v}}}}});

const uv=D.pnl.map(r=>r.unrealized_pnl);
new Chart(document.getElementById('upnlChart'),{type:'bar',
  data:{labels:pnlSyms,datasets:[{data:uv,
    backgroundColor:uv.map(v=>v>=0?'rgba(59,130,246,.7)':'rgba(239,68,68,.7)'),borderRadius:4}]},
  options:{...base,scales:{...base.scales,y:{...base.scales.y,ticks:{...base.scales.y.ticks,callback:v=>'$'+v}}}}});

document.querySelectorAll('table').forEach(table=>{
  const ths=table.querySelectorAll('th');let sc=-1,sa=true;
  ths.forEach((th,col)=>{th.addEventListener('click',()=>{
    if(sc===col){sa=!sa;}else{sc=col;sa=true;}
    ths.forEach(h=>h.classList.remove('sort-asc','sort-desc'));
    th.classList.add(sa?'sort-asc':'sort-desc');
    const tb=table.querySelector('tbody');
    Array.from(tb.querySelectorAll('tr')).sort((a,b)=>{
      const at=a.cells[col]?.innerText.trim()??'',bt=b.cells[col]?.innerText.trim()??'';
      const sv=t=>{if(/^\\d{4}-\\d{2}-\\d{2}/.test(t))return new Date(t.replace(' ','T')).getTime();
        const n=parseFloat(t.replace(/[^0-9.\\-]/g,''));return isNaN(n)?t:n;};
      const av=sv(at),bv=sv(bt);const cmp=typeof av==='string'?av.localeCompare(bv):av-bv;
      return sa?cmp:-cmp;
    }).forEach(r=>tb.appendChild(r));
  });});
});
</script>
</body>
</html>"""


def main():
    c = RobinhoodClient()
    print("Fetching Robinhood data...")
    data = build_data(c)

    def sign(v):
        return ("+$" if v >= 0 else "-$") + f"{abs(v):.2f}"

    html = HTML.replace("{{data_json}}",      json.dumps(data))
    html = html.replace("{{generated_at}}",   data["generated_at"])
    html = html.replace("{{total_value}}",    str(data["total_value"]))
    html = html.replace("{{upnl_str}}",       sign(data["total_unrealized"]))
    html = html.replace("{{rpnl_str}}",       sign(data["total_realized"]))
    html = html.replace("{{upnl_class}}",     "green" if data["total_unrealized"] >= 0 else "red")
    html = html.replace("{{rpnl_class}}",     "green" if data["total_realized"]   >= 0 else "red")
    html = html.replace("{{total_invested}}", str(data["total_invested"]))
    html = html.replace("{{total_cost}}",     str(data["total_cost"]))
    html = html.replace("{{num_assets}}",     str(data["num_assets"]))
    html = html.replace("{{order_count}}",    str(len(data["orders"])))

    out = os.path.join(tempfile.gettempdir(), "robinhood_dashboard.html")
    with open(out, "w") as f:
        f.write(html)

    print(f"Dashboard saved to {out}")
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
