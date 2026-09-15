"""
Overlay Kalshi bet outcomes with BTC technical indicators (RSI-14, EMA-20, EMA-50)
to find which market conditions produce the best win rate and P&L.

Run with: op run --env-file=.env -- python3 -m kalshi.btc_analysis
"""
import json
import math
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

CT = ZoneInfo("America/Chicago")
BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ── Indicator math ────────────────────────────────────────────────────────────

def ema(prices: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    seed = sum(prices[:period]) / period
    result.append(seed)
    prev = seed
    for p in prices[period:]:
        val = p * k + prev * (1 - k)
        result.append(val)
        prev = val
    return result


def rsi(prices: list[float], period: int = 14) -> list[float]:
    result = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss else float("inf")
    result.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(prices)):
        d = prices[i] - prices[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        result.append(100 - 100 / (1 + rs))
    return result


def daily_change_pct(prices: list[float], timestamps: list[int], idx: int) -> float:
    """% change vs price 24 hours before index idx."""
    ts = timestamps[idx]
    target = ts - 86400_000  # 24h in ms
    best = min(range(len(timestamps)), key=lambda i: abs(timestamps[i] - target))
    ref = prices[best]
    return (prices[idx] - ref) / ref * 100 if ref else 0


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_btc_hourly(days: int = 90) -> tuple[list[int], list[float]]:
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    r = requests.get(url, params={"vs_currency": "usd", "days": days, "interval": "hourly"}, timeout=20)
    data = r.json()["prices"]
    ts   = [int(d[0]) for d in data]
    px   = [d[1] for d in data]
    return ts, px


def fetch_kalshi_data(session) -> list[dict]:
    """Pull all settled bets with fill timestamps."""
    def paginate(url, key):
        results, cursor = [], None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            d = session.get(url, params=params).json()
            results.extend(d.get(key, []))
            cursor = d.get("cursor")
            if not cursor or len(d.get(key, [])) < 100:
                break
        return results

    settlements = paginate(f"{BASE}/portfolio/settlements", "settlements")
    fills_raw   = paginate(f"{BASE}/portfolio/fills", "fills")

    def norm_dt(s):
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    fills_by_ticker = defaultdict(list)
    for f in fills_raw:
        fills_by_ticker[f["market_ticker"]].append(f)

    bets = []
    for s in settlements:
        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty  = float(s.get("no_count_fp", 0))
        result  = s.get("market_result", "")
        revenue = s.get("revenue", 0) / 100
        side    = "YES" if yes_qty > 0 else "NO"
        cost    = float(s.get("yes_total_cost_dollars" if yes_qty > 0 else "no_total_cost_dollars", 0))
        won     = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        pnl     = revenue - cost if won else -cost

        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if not ticker_fills:
            continue
        earliest  = min(ticker_fills, key=lambda f: f["created_time"])
        placed_dt = norm_dt(earliest["created_time"])
        placed_ts = int(placed_dt.timestamp() * 1000)

        bets.append({
            "ticker":    s["ticker"],
            "placed_ts": placed_ts,
            "placed_ct": placed_dt.astimezone(CT).strftime("%Y-%m-%d %H:%M"),
            "side":      side,
            "won":       won,
            "pnl":       round(pnl, 2),
            "cost":      round(cost, 2),
        })

    return bets


# ── Indicator lookup ──────────────────────────────────────────────────────────

def nearest_idx(timestamps: list[int], target_ts: int) -> int:
    return min(range(len(timestamps)), key=lambda i: abs(timestamps[i] - target_ts))


def label_rsi(r: float) -> str:
    if r is None:   return "unknown"
    if r < 30:      return "oversold (<30)"
    if r < 45:      return "mild-bearish (30-45)"
    if r <= 55:     return "neutral (45-55)"
    if r <= 70:     return "mild-bullish (55-70)"
    return "overbought (>70)"


def label_trend(price: float, e20: float, e50: float) -> str:
    if e20 is None or e50 is None: return "unknown"
    if price > e20 > e50:   return "strong-up"
    if price > e20:         return "above-ema20"
    if price > e50:         return "between-emas"
    return "below-both"


def label_daily(chg: float) -> str:
    if chg > 2:    return "strong-up-day (>+2%)"
    if chg > 0.5:  return "up-day (+0.5–2%)"
    if chg > -0.5: return "flat-day"
    if chg > -2:   return "down-day (-0.5–2%)"
    return "strong-down-day (<-2%)"


# ── Analysis ──────────────────────────────────────────────────────────────────

def correlate(bets: list[dict], field: str) -> dict:
    buckets = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for b in bets:
        k = b.get(field, "unknown")
        buckets[k]["bets"] += 1
        buckets[k]["wins"] += int(b["won"])
        buckets[k]["pnl"]  += b["pnl"]
    return dict(buckets)


# ── HTML template ─────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Kalshi × BTC Technical Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
    --muted:#94a3b8;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:24px;}
  h1{font-size:20px;font-weight:600;margin-bottom:4px;}
  .subtitle{color:var(--muted);margin-bottom:28px;font-size:12px;}

  .reco{border-radius:12px;padding:20px 24px;margin-bottom:28px;border:2px solid;}
  .reco.go   {background:rgba(34,197,94,.1); border-color:var(--green);}
  .reco.nogo {background:rgba(239,68,68,.1); border-color:var(--red);}
  .reco.wait {background:rgba(245,158,11,.1);border-color:var(--yellow);}
  .reco h2{font-size:18px;font-weight:700;margin-bottom:8px;}
  .reco ul{margin-left:16px;line-height:2;}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px;}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-bottom:28px;}
  .box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;}
  .box h3{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;}
  .chart-wrap{position:relative;height:200px;}

  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px;}
  .stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;}
  .stat-label{color:var(--muted);font-size:11px;text-transform:uppercase;margin-bottom:6px;}
  .stat-val{font-size:20px;font-weight:700;}
  .green{color:var(--green);}.red{color:var(--red);}.blue{color:var(--blue);}.yellow{color:var(--yellow);}

  .section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px;overflow-x:auto;}
  .section h3{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;}
  table{width:100%;border-collapse:collapse;}
  th{color:var(--muted);text-align:left;padding:5px 10px;font-size:11px;text-transform:uppercase;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:rgba(255,255,255,.03);}
  .num{text-align:right;font-variant-numeric:tabular-nums;}
  .badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:11px;font-weight:600;}
  .badge.win {background:rgba(34,197,94,.15);color:var(--green);}
  .badge.loss{background:rgba(239,68,68,.15);color:var(--red);}
</style>
</head>
<body>
<h1>Kalshi × BTC Technical Overlay</h1>
<p class="subtitle">Generated {{generated_at}} · {{n_bets}} bets analysed</p>

<div class="reco {{reco_class}}">
  <h2>{{reco_title}}</h2>
  <ul>{{reco_bullets}}</ul>
</div>

<div class="stats">
  <div class="stat"><div class="stat-label">BTC Now</div><div class="stat-val blue">${{btc_now}}</div></div>
  <div class="stat"><div class="stat-label">RSI-14</div><div class="stat-val {{rsi_class}}">{{rsi_now}}</div></div>
  <div class="stat"><div class="stat-label">vs EMA-20</div><div class="stat-val {{ema20_class}}">{{ema20_diff}}</div></div>
  <div class="stat"><div class="stat-label">24h Move</div><div class="stat-val {{daily_class}}">{{daily_chg}}</div></div>
</div>

<div class="grid2">
  <div class="box"><h3>BTC Price + EMA-20/50 (hourly)</h3><div class="chart-wrap"><canvas id="priceChart"></canvas></div></div>
  <div class="box"><h3>RSI-14 (hourly)</h3><div class="chart-wrap"><canvas id="rsiChart"></canvas></div></div>
</div>

<div class="grid3">
  <div class="box"><h3>Win Rate by RSI State</h3><div class="chart-wrap"><canvas id="rsiWinChart"></canvas></div></div>
  <div class="box"><h3>P&amp;L by RSI State</h3><div class="chart-wrap"><canvas id="rsiPnlChart"></canvas></div></div>
  <div class="box"><h3>Win Rate by Trend State</h3><div class="chart-wrap"><canvas id="trendChart"></canvas></div></div>
</div>

<div class="grid2">
  <div class="box"><h3>Win Rate by Daily Move</h3><div class="chart-wrap"><canvas id="dailyWinChart"></canvas></div></div>
  <div class="box"><h3>P&amp;L by Daily Move</h3><div class="chart-wrap"><canvas id="dailyPnlChart"></canvas></div></div>
</div>

<div class="section">
  <h3>All Bets with Indicators at Placement</h3>
  <table>
    <thead><tr>
      <th>Placed (CT)</th><th>Ticker</th><th>Side</th><th>Result</th>
      <th class="num">BTC Price</th><th class="num">RSI</th><th>RSI State</th>
      <th>Trend</th><th>24h Chg</th><th class="num">P&amp;L</th>
    </tr></thead>
    <tbody id="betsTbody"></tbody>
  </table>
</div>

<script>
const D = {{data_json}};
const GC='rgba(255,255,255,.07)', TC='#64748b';

// Price + EMA chart (last 72 hours)
const pc = D.price_chart;
new Chart(document.getElementById('priceChart'),{type:'line',data:{
  labels:pc.labels,
  datasets:[
    {label:'BTC',data:pc.price,borderColor:'#e2e8f0',borderWidth:1.5,pointRadius:0,tension:0},
    {label:'EMA20',data:pc.ema20,borderColor:'#3b82f6',borderWidth:1.5,pointRadius:0,tension:0,borderDash:[4,2]},
    {label:'EMA50',data:pc.ema50,borderColor:'#f59e0b',borderWidth:1.5,pointRadius:0,tension:0,borderDash:[4,2]},
  ]},
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:true,labels:{color:TC,font:{size:11}}},tooltip:{mode:'index',intersect:false}},
    scales:{x:{grid:{color:GC},ticks:{color:TC,maxTicksLimit:8}},
            y:{grid:{color:GC},ticks:{color:TC,callback:v=>'$'+v.toLocaleString()}}}}
});

// RSI chart
new Chart(document.getElementById('rsiChart'),{type:'line',data:{
  labels:pc.labels,
  datasets:[{label:'RSI-14',data:pc.rsi,borderColor:'#a855f7',borderWidth:1.5,pointRadius:0,tension:0}]},
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},
      annotation:{annotations:{ob:{type:'line',y:70,borderColor:'rgba(239,68,68,.4)',borderWidth:1},
                               os:{type:'line',y:30,borderColor:'rgba(34,197,94,.4)',borderWidth:1}}}},
    scales:{x:{grid:{color:GC},ticks:{color:TC,maxTicksLimit:8}},
            y:{min:0,max:100,grid:{color:GC},ticks:{color:TC}}}}
});

const COLORS=['#3b82f6','#22c55e','#f59e0b','#a855f7','#f97316','#06b6d4','#ef4444'];
function barChart(id,labels,data,colorFn){
  new Chart(document.getElementById(id),{type:'bar',data:{labels,datasets:[{data,
    backgroundColor:colorFn?data.map(colorFn):COLORS,borderRadius:3}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{grid:{color:GC},ticks:{color:TC,font:{size:10}}},
              y:{grid:{color:GC},ticks:{color:TC}}}}});
}

const rsi_labels = D.rsi_corr.map(r=>r.label);
barChart('rsiWinChart', rsi_labels, D.rsi_corr.map(r=>r.win_rate), v=>v>=50?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)');
barChart('rsiPnlChart', rsi_labels, D.rsi_corr.map(r=>r.pnl), v=>v>=0?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)');
barChart('trendChart', D.trend_corr.map(r=>r.label), D.trend_corr.map(r=>r.win_rate), v=>v>=50?'rgba(59,130,246,.7)':'rgba(239,68,68,.7)');
barChart('dailyWinChart', D.daily_corr.map(r=>r.label), D.daily_corr.map(r=>r.win_rate), v=>v>=50?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)');
barChart('dailyPnlChart', D.daily_corr.map(r=>r.label), D.daily_corr.map(r=>r.pnl), v=>v>=0?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)');

// Bets table
D.bets.forEach(b=>{
  const pnl=(b.pnl>=0?'+$':'-$')+Math.abs(b.pnl).toFixed(2);
  const pnlCls=b.pnl>=0?'green':'red';
  const chg=(b.daily_chg>=0?'+':'')+b.daily_chg.toFixed(1)+'%';
  document.getElementById('betsTbody').innerHTML+=`<tr>
    <td class="muted">${b.placed_ct}</td>
    <td>${b.ticker.substring(0,22)}</td>
    <td>${b.side}</td>
    <td><span class="badge ${b.won?'win':'loss'}">${b.won?'WIN':'LOSS'}</span></td>
    <td class="num">$${b.btc_price.toLocaleString(undefined,{maximumFractionDigits:0})}</td>
    <td class="num">${b.rsi!==null?b.rsi.toFixed(1):'—'}</td>
    <td class="muted" style="font-size:11px">${b.rsi_label}</td>
    <td class="muted" style="font-size:11px">${b.trend_label}</td>
    <td class="${b.daily_chg>=0?'green':'red'}">${chg}</td>
    <td class="num ${pnlCls}">${pnl}</td>
  </tr>`;
});
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from kalshi import KalshiClient
    c = KalshiClient()

    print("Fetching BTC hourly data (90 days)...")
    ts_list, px_list = fetch_btc_hourly(90)

    print("Computing indicators...")
    ema20_list = ema(px_list, 20)
    ema50_list = ema(px_list, 50)
    rsi_list   = rsi(px_list, 14)

    print("Fetching Kalshi bet history...")
    bets = fetch_kalshi_data(c.session)

    # Attach indicator values to each bet
    for b in bets:
        idx = nearest_idx(ts_list, b["placed_ts"])
        b["btc_price"]  = round(px_list[idx], 2)
        b["rsi"]        = rsi_list[idx]
        b["ema20"]      = ema20_list[idx]
        b["ema50"]      = ema50_list[idx]
        b["daily_chg"]  = round(daily_change_pct(px_list, ts_list, idx), 2)
        b["rsi_label"]  = label_rsi(b["rsi"])
        b["trend_label"]= label_trend(b["btc_price"], b["ema20"], b["ema50"])
        b["daily_label"]= label_daily(b["daily_chg"])

    # Correlation tables
    def corr_table(field, order=None):
        raw = correlate(bets, field)
        rows = []
        for label, d in raw.items():
            rows.append({
                "label":    label,
                "bets":     d["bets"],
                "wins":     d["wins"],
                "win_rate": round(d["wins"] / d["bets"] * 100, 1) if d["bets"] else 0,
                "pnl":      round(d["pnl"], 2),
            })
        if order:
            rows.sort(key=lambda r: order.index(r["label"]) if r["label"] in order else 99)
        else:
            rows.sort(key=lambda r: -r["pnl"])
        return rows

    rsi_order   = ["oversold (<30)","mild-bearish (30-45)","neutral (45-55)","mild-bullish (55-70)","overbought (>70)"]
    trend_order = ["strong-up","above-ema20","between-emas","below-both"]
    daily_order = ["strong-up-day (>+2%)","up-day (+0.5–2%)","flat-day","down-day (-0.5–2%)","strong-down-day (<-2%)"]

    rsi_corr   = corr_table("rsi_label",   rsi_order)
    trend_corr = corr_table("trend_label", trend_order)
    daily_corr = corr_table("daily_label", daily_order)

    # Current state
    cur_rsi   = rsi_list[-1]
    cur_price = px_list[-1]
    cur_ema20 = ema20_list[-1]
    cur_ema50 = ema50_list[-1]
    cur_daily = round(daily_change_pct(px_list, ts_list, len(ts_list) - 1), 2)
    cur_rsi_label   = label_rsi(cur_rsi)
    cur_trend_label = label_trend(cur_price, cur_ema20, cur_ema50)
    cur_daily_label = label_daily(cur_daily)

    # Find best conditions from history
    best_rsi_row   = max(rsi_corr,   key=lambda r: r["pnl"]) if rsi_corr   else None
    best_trend_row = max(trend_corr, key=lambda r: r["pnl"]) if trend_corr else None

    # Current RSI and trend stats from historical data
    cur_rsi_hist   = next((r for r in rsi_corr   if r["label"] == cur_rsi_label),   None)
    cur_trend_hist = next((r for r in trend_corr if r["label"] == cur_trend_label), None)

    # Build recommendation
    score = 0
    bullets = []
    if cur_rsi_hist:
        wr = cur_rsi_hist["win_rate"]
        pnl = cur_rsi_hist["pnl"]
        if pnl > 0:
            score += 1
            bullets.append(f"RSI {cur_rsi:.1f} ({cur_rsi_label}) — historically {wr:.0f}% win rate, {'+' if pnl>0 else ''}{pnl:.2f} P&L across {cur_rsi_hist['bets']} bets ✅")
        else:
            score -= 1
            bullets.append(f"RSI {cur_rsi:.1f} ({cur_rsi_label}) — historically {wr:.0f}% win rate but {pnl:.2f} P&L ❌")
    if cur_trend_hist:
        wr = cur_trend_hist["win_rate"]
        pnl = cur_trend_hist["pnl"]
        if pnl > 0 and wr >= 50:
            score += 1
            bullets.append(f"Trend: {cur_trend_label} — {wr:.0f}% win, {'+' if pnl>=0 else ''}{pnl:.2f} P&L ✅")
        else:
            score -= 1
            bullets.append(f"Trend: {cur_trend_label} — {wr:.0f}% win, {pnl:.2f} P&L ❌")
    if cur_daily > 0.5:
        score += 1
        bullets.append(f"24h move: {cur_daily:+.1f}% — positive day, favors YES bets ✅")
    elif cur_daily < -0.5:
        score -= 1
        bullets.append(f"24h move: {cur_daily:+.1f}% — negative day, favors NO bets ❌")
    else:
        bullets.append(f"24h move: {cur_daily:+.1f}% — flat, neutral signal ⚠️")

    if best_rsi_row and best_rsi_row["label"] != cur_rsi_label:
        bullets.append(f"Best historical RSI zone is '{best_rsi_row['label']}' (+{best_rsi_row['pnl']:.2f} P&L) — not currently there")

    if score >= 2:
        reco_class = "go"
        reco_title = "🟢 GO — Conditions favour placing a bet"
        side_hint  = "YES" if cur_price > cur_ema20 else "NO"
        bullets.append(f"Suggested side: {side_hint} (price {'above' if cur_price > cur_ema20 else 'below'} EMA-20)")
    elif score <= -1:
        reco_class = "nogo"
        reco_title = "🔴 NO-GO — Conditions unfavourable"
    else:
        reco_class = "wait"
        reco_title = "🟡 WAIT — Mixed signals, no edge"

    # Price chart data — last 72 hours
    n = min(72, len(ts_list))
    chart_ts   = ts_list[-n:]
    chart_px   = px_list[-n:]
    chart_e20  = ema20_list[-n:]
    chart_e50  = ema50_list[-n:]
    chart_rsi  = rsi_list[-n:]
    chart_labels = [
        datetime.fromtimestamp(t / 1000, tz=CT).strftime("%m/%d %H:%M")
        for t in chart_ts
    ]

    data_json = {
        "bets":       sorted(bets, key=lambda b: b["placed_ts"], reverse=True),
        "rsi_corr":   rsi_corr,
        "trend_corr": trend_corr,
        "daily_corr": daily_corr,
        "price_chart": {
            "labels": chart_labels,
            "price":  [round(p, 2) for p in chart_px],
            "ema20":  [round(v, 2) if v else None for v in chart_e20],
            "ema50":  [round(v, 2) if v else None for v in chart_e50],
            "rsi":    [round(v, 1) if v else None for v in chart_rsi],
        },
    }

    # Render HTML
    ema20_diff = round((cur_price / cur_ema20 - 1) * 100, 2) if cur_ema20 else 0
    html = HTML.replace("{{data_json}}",    json.dumps(data_json))
    html = html.replace("{{generated_at}}", datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"))
    html = html.replace("{{n_bets}}",       str(len(bets)))
    html = html.replace("{{reco_class}}",   reco_class)
    html = html.replace("{{reco_title}}",   reco_title)
    html = html.replace("{{reco_bullets}}", "".join(f"<li>{b}</li>" for b in bullets))
    html = html.replace("{{btc_now}}",      f"{cur_price:,.0f}")
    html = html.replace("{{rsi_now}}",      f"{cur_rsi:.1f}" if cur_rsi else "—")
    html = html.replace("{{rsi_class}}",    "red" if cur_rsi and cur_rsi > 70 else ("green" if cur_rsi and cur_rsi < 30 else "yellow"))
    html = html.replace("{{ema20_diff}}",   f"{ema20_diff:+.1f}%")
    html = html.replace("{{ema20_class}}",  "green" if ema20_diff > 0 else "red")
    html = html.replace("{{daily_chg}}",    f"{cur_daily:+.1f}%")
    html = html.replace("{{daily_class}}",  "green" if cur_daily > 0 else "red")

    out = os.path.join(tempfile.gettempdir(), "kalshi_btc_analysis.html")
    with open(out, "w") as f:
        f.write(html)

    print(f"\nCurrent state:")
    print(f"  BTC:     ${cur_price:,.2f}")
    print(f"  RSI-14:  {cur_rsi:.1f} ({cur_rsi_label})")
    print(f"  Trend:   {cur_trend_label}")
    print(f"  24h:     {cur_daily:+.1f}%")
    print(f"\nRecommendation: {reco_title}")
    for b in bullets:
        print(f"  • {b}")
    print(f"\nDashboard: {out}")
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
