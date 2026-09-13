"""
Correlate Kalshi bet outcomes with BTC/ETH technical conditions at placement time.
Generates an HTML analysis report and opens it.
Run with: op run --env-file=.env -- python3 -m kalshi.analysis
"""
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import robin_stocks.robinhood as rh
from kalshi import KalshiClient

CT  = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


# ─── helpers ─────────────────────────────────────────────────────────────────

def parse_dt(s):
    if not s: return None
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def to_ct(dt): return dt.astimezone(CT)

def compute_rsi(closes, period=14):
    if len(closes) < period + 1: return [None] * len(closes)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    rsi = [None] * (period + 1)
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al else float('inf')
        rsi.append(round(100 - 100 / (1 + rs), 1))
    return rsi

def compute_ema(closes, period):
    if len(closes) < period: return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [None] * (period - 1) + [sum(closes[:period]) / period]
    for i in range(period, len(closes)):
        ema.append(round(closes[i] * k + ema[-1] * (1 - k), 4))
    return ema

def compute_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]['high_price'])
        l = float(candles[i]['low_price'])
        pc = float(candles[i-1]['close_price'])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period: return [None] * len(candles)
    atr = [None] * period + [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr.append((atr[-1] * (period - 1) + trs[i]) / period)
    return atr


# ─── data fetching ────────────────────────────────────────────────────────────

def fetch_candles(symbol):
    raw = rh.crypto.get_crypto_historicals(symbol, interval='hour', span='3month') or []
    candles = []
    for c in raw:
        dt = parse_dt(c['begins_at'])
        if dt:
            candles.append({
                'dt':    dt,
                'open':  float(c['open_price']),
                'high':  float(c['high_price']),
                'low':   float(c['low_price']),
                'close': float(c['close_price']),
                'vol':   float(c.get('volume', 0)),
                'high_price':  c['high_price'],
                'low_price':   c['low_price'],
                'close_price': c['close_price'],
            })
    return candles

def enrich_candles(candles):
    closes = [c['close'] for c in candles]
    rsi14  = compute_rsi(closes, 14)
    ema8   = compute_ema(closes, 8)
    ema21  = compute_ema(closes, 21)
    ema50  = compute_ema(closes, 50)
    atr14  = compute_atr(candles, 14)
    for i, c in enumerate(candles):
        c['rsi']   = rsi14[i]
        c['ema8']  = ema8[i]
        c['ema21'] = ema21[i]
        c['ema50'] = ema50[i]
        c['atr']   = atr14[i]
        # price vs ema21
        if c['ema21']:
            c['trend'] = 'bullish' if c['close'] > c['ema21'] else 'bearish'
        else:
            c['trend'] = None
        # ema8 vs ema21 alignment
        if c['ema8'] and c['ema21']:
            c['alignment'] = 'bullish' if c['ema8'] > c['ema21'] else 'bearish'
        else:
            c['alignment'] = None
    return candles

def find_candle(candles, dt):
    """Return the enriched candle whose hour contains dt."""
    target = dt.replace(minute=0, second=0, microsecond=0)
    for c in reversed(candles):
        if c['dt'] <= dt:
            return c
    return None


# ─── main analysis ────────────────────────────────────────────────────────────

def fetch_settlements_with_fills(c):
    base = "https://api.elections.kalshi.com/trade-api/v2"
    settlements, cursor = [], None
    while True:
        params = {"limit": 100}
        if cursor: params["cursor"] = cursor
        data = c.session.get(f"{base}/portfolio/settlements", params=params).json()
        settlements.extend(data.get("settlements", []))
        cursor = data.get("cursor")
        if not cursor or len(data.get("settlements", [])) < 100: break

    fills_raw = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor: params["cursor"] = cursor
        data = c.session.get(f"{base}/portfolio/fills", params=params).json()
        fills_raw.extend(data.get("fills", []))
        cursor = data.get("cursor")
        if not cursor or len(data.get("fills", [])) < 100: break

    fills_by_ticker = defaultdict(list)
    for f in fills_raw:
        fills_by_ticker[f["market_ticker"]].append(f)

    return settlements, fills_by_ticker


def rsi_label(rsi):
    if rsi is None:          return "unknown"
    if rsi < 30:             return "oversold (<30)"
    if rsi < 45:             return "low (30–45)"
    if rsi < 55:             return "neutral (45–55)"
    if rsi < 70:             return "high (55–70)"
    return                          "overbought (>70)"


def build_analysis():
    print("Logging into Robinhood...")
    rh.login(username=os.environ["ROBINHOOD_USERNAME"],
             password=os.environ["ROBINHOOD_PASSWORD"],
             store_session=True)

    print("Fetching BTC & ETH hourly candles (3 months)...")
    btc_candles = enrich_candles(fetch_candles("BTC"))
    eth_candles = enrich_candles(fetch_candles("ETH"))
    price_data  = {"BTC": btc_candles, "ETH": eth_candles}

    print("Fetching Kalshi history...")
    kc = KalshiClient()
    settlements, fills_by_ticker = fetch_settlements_with_fills(kc)

    # Build enriched bet rows
    bets = []
    for s in settlements:
        ticker  = s["ticker"]
        sym     = "BTC" if "BTC" in ticker else ("ETH" if "ETH" in ticker else None)
        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty  = float(s.get("no_count_fp", 0))
        result  = s.get("market_result", "")
        revenue = s.get("revenue", 0) / 100
        side    = "YES" if yes_qty > 0 else "NO"
        cost    = float(s.get("yes_total_cost_dollars" if yes_qty > 0 else "no_total_cost_dollars", 0))
        won     = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        pnl     = revenue - cost if won else -cost

        fills   = fills_by_ticker.get(ticker, [])
        placed_dt = parse_dt(min(fills, key=lambda f: f["created_time"])["created_time"]) if fills else parse_dt(s["settled_time"])
        placed_ct = to_ct(placed_dt)

        candle  = find_candle(price_data.get(sym, []), placed_dt) if sym else None

        bets.append({
            "ticker":     ticker,
            "sym":        sym,
            "side":       side,
            "won":        won,
            "pnl":        round(pnl, 2),
            "cost":       round(cost, 2),
            "placed_ct":  placed_ct.strftime("%Y-%m-%d %H:%M"),
            "hour_ct":    placed_ct.hour,
            "rsi":        candle["rsi"]   if candle else None,
            "ema8":       candle["ema8"]  if candle else None,
            "ema21":      candle["ema21"] if candle else None,
            "trend":      candle["trend"] if candle else None,
            "alignment":  candle["alignment"] if candle else None,
            "atr":        candle["atr"]   if candle else None,
            "price":      candle["close"] if candle else None,
            "rsi_label":  rsi_label(candle["rsi"] if candle else None),
        })

    # Filter to BTC/ETH bets only for technical analysis
    ta_bets = [b for b in bets if b["sym"] in ("BTC", "ETH")]

    def bucket_stats(group_key_fn, bets):
        stats = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
        for b in bets:
            k = group_key_fn(b)
            stats[k]["bets"] += 1
            stats[k]["wins"] += int(b["won"])
            stats[k]["pnl"]  += b["pnl"]
        return {k: {**v, "win_pct": round(v["wins"]/v["bets"]*100, 0), "avg_pnl": round(v["pnl"]/v["bets"], 2)}
                for k, v in stats.items() if v["bets"] >= 1}

    rsi_stats   = bucket_stats(lambda b: b["rsi_label"], ta_bets)
    trend_stats = bucket_stats(lambda b: b["trend"] or "unknown", ta_bets)
    align_stats = bucket_stats(lambda b: b["alignment"] or "unknown", ta_bets)
    hour_stats  = bucket_stats(lambda b: b["hour_ct"], ta_bets)
    side_stats  = bucket_stats(lambda b: b["side"], ta_bets)

    # Identify best/worst conditions
    def signal(win_pct, n):
        if n < 2: return "insufficient data"
        if win_pct >= 70: return "✅ BET"
        if win_pct <= 35: return "❌ SIT OUT"
        return "⚠️  COIN FLIP"

    return {
        "bets":       bets,
        "ta_bets":    ta_bets,
        "rsi_stats":  rsi_stats,
        "trend_stats": trend_stats,
        "align_stats": align_stats,
        "hour_stats": hour_stats,
        "side_stats": side_stats,
        "n_total":    len(bets),
        "n_ta":       len(ta_bets),
        "generated":  datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"),
        "signal_fn":  signal,
    }


# ─── HTML report ─────────────────────────────────────────────────────────────

def stat_row(label, s, signal_fn):
    wp = s["win_pct"]
    color = "green" if wp >= 70 else ("red" if wp <= 35 else "yellow")
    pnl_s = ("+$" if s["avg_pnl"] >= 0 else "-$") + f"{abs(s['avg_pnl']):.2f}"
    sig   = signal_fn(wp, s["bets"])
    return (f"<tr><td>{label}</td><td class='num'>{s['bets']}</td>"
            f"<td class='num'>{s['wins']}</td>"
            f"<td class='num {color}'>{wp:.0f}%</td>"
            f"<td class='num'>{pnl_s}</td>"
            f"<td style='font-size:14px'>{sig}</td></tr>")


def build_html(d):
    signal = d["signal_fn"]

    # RSI rows ordered
    rsi_order = ["oversold (<30)", "low (30–45)", "neutral (45–55)", "high (55–70)", "overbought (>70)", "unknown"]
    rsi_rows = "".join(stat_row(k, d["rsi_stats"][k], signal) for k in rsi_order if k in d["rsi_stats"])

    trend_rows = "".join(stat_row(k.title(), d["trend_stats"][k], signal) for k in sorted(d["trend_stats"]))
    align_rows = "".join(stat_row(k.title(), d["align_stats"][k], signal) for k in sorted(d["align_stats"]))
    hour_rows  = "".join(stat_row(f"{k:02d}:00 CT", d["hour_stats"][k], signal) for k in sorted(d["hour_stats"]))

    # Bet table with technicals
    bet_rows = ""
    for b in sorted(d["ta_bets"], key=lambda x: x["placed_ct"], reverse=True):
        wp   = "win" if b["won"] else "loss"
        rsi  = f"{b['rsi']:.1f}" if b["rsi"] else "—"
        e21  = f"${b['ema21']:,.0f}" if b["ema21"] else "—"
        prc  = f"${b['price']:,.0f}" if b["price"] else "—"
        tr   = b["trend"] or "—"
        tr_c = "green" if tr == "bullish" else ("red" if tr == "bearish" else "muted")
        pnl_c = "green" if b["pnl"] >= 0 else "red"
        pnl_s = ("+$" if b["pnl"] >= 0 else "-$") + f"{abs(b['pnl']):.2f}"
        side_cls  = "" if b["side"] == "YES" else "no"
        rsi_val   = b["rsi"]
        rsi_cls   = "red" if rsi_val and rsi_val > 70 else ("green" if rsi_val and rsi_val < 30 else "")
        bet_rows += (f"<tr><td class='muted'>{b['placed_ct']}</td>"
                     f"<td>{b['sym']}</td><td><span class='badge {wp}'>{wp.upper()}</span></td>"
                     f"<td><span class='tag {side_cls}'>{b['side']}</span></td>"
                     f"<td class='num'>{prc}</td><td class='num'>{e21}</td>"
                     f"<td class='num {rsi_cls}'>{rsi}</td>"
                     f"<td class='{tr_c}'>{tr}</td>"
                     f"<td class='num {pnl_c}'>{pnl_s}</td></tr>")

    # Chart data
    rsi_labels = [k for k in rsi_order if k in d["rsi_stats"]]
    rsi_wps    = [d["rsi_stats"][k]["win_pct"] for k in rsi_labels]
    rsi_ns     = [d["rsi_stats"][k]["bets"] for k in rsi_labels]

    hour_labels = sorted(d["hour_stats"])
    hour_wps    = [d["hour_stats"][h]["win_pct"] for h in hour_labels]
    hour_ns     = [d["hour_stats"][h]["bets"] for h in hour_labels]
    hour_pnls   = [round(d["hour_stats"][h]["pnl"], 2) for h in hour_labels]

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi Betting Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;--muted:#94a3b8;
        --green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#f59e0b;--purple:#a855f7}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:24px}}
  h1{{font-size:20px;font-weight:600;margin-bottom:4px}}
  h2{{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}}
  .subtitle{{color:var(--muted);margin-bottom:28px;font-size:12px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}}
  .grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-bottom:24px}}
  .box{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px}}
  .chart-wrap{{position:relative;height:200px}}
  .full{{grid-column:1/-1}}
  table{{width:100%;border-collapse:collapse}}
  th{{color:var(--muted);text-align:left;padding:6px 12px;font-size:11px;text-transform:uppercase;
      letter-spacing:.06em;border-bottom:1px solid var(--border);white-space:nowrap}}
  td{{padding:7px 12px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:rgba(255,255,255,.03)}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .green{{color:var(--green)}} .red{{color:var(--red)}} .yellow{{color:var(--yellow)}} .muted{{color:var(--muted)}}
  .badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}}
  .badge.win{{background:rgba(34,197,94,.15);color:var(--green)}}
  .badge.loss{{background:rgba(239,68,68,.15);color:var(--red)}}
  .tag{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;background:rgba(59,130,246,.15);color:var(--blue)}}
  .tag.no{{background:rgba(168,85,247,.15);color:var(--purple)}}
  .insight{{background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.2);border-radius:8px;
            padding:14px 18px;margin-bottom:12px;line-height:1.8}}
  .insight strong{{color:var(--blue)}}
  @media(max-width:768px){{.grid2,.grid3{{grid-template-columns:1fr}}}}
</style></head><body>

<h1>Kalshi Betting Pattern Analysis</h1>
<p class="subtitle">Generated {d['generated']} &nbsp;·&nbsp; {d['n_ta']} BTC/ETH bets analyzed against hourly technicals</p>

<div class="grid2">
  <div class="box full">
    <h2>Key Insights</h2>
    {''.join(build_insights(d))}
  </div>
</div>

<div class="grid2">
  <div class="box">
    <h2>Win Rate by RSI (hourly)</h2>
    <div class="chart-wrap"><canvas id="rsiChart"></canvas></div>
  </div>
  <div class="box">
    <h2>P&amp;L &amp; Win Rate by Hour (CT)</h2>
    <div class="chart-wrap"><canvas id="hourChart"></canvas></div>
  </div>
</div>

<div class="grid3">
  <div class="box">
    <h2>By RSI Bucket</h2>
    <table><thead><tr><th>RSI</th><th class="num">N</th><th class="num">W</th><th class="num">Win%</th><th class="num">Avg P&L</th><th>Signal</th></tr></thead>
    <tbody>{rsi_rows}</tbody></table>
  </div>
  <div class="box">
    <h2>Price vs EMA-21</h2>
    <table><thead><tr><th>Trend</th><th class="num">N</th><th class="num">W</th><th class="num">Win%</th><th class="num">Avg P&L</th><th>Signal</th></tr></thead>
    <tbody>{trend_rows}</tbody></table>
    <br>
    <h2>EMA-8 vs EMA-21</h2>
    <table><thead><tr><th>Alignment</th><th class="num">N</th><th class="num">W</th><th class="num">Win%</th><th class="num">Avg P&L</th><th>Signal</th></tr></thead>
    <tbody>{align_rows}</tbody></table>
  </div>
  <div class="box">
    <h2>By Hour of Day (CT)</h2>
    <table><thead><tr><th>Hour</th><th class="num">N</th><th class="num">W</th><th class="num">Win%</th><th class="num">Avg P&L</th><th>Signal</th></tr></thead>
    <tbody>{hour_rows}</tbody></table>
  </div>
</div>

<div class="box" style="margin-bottom:24px">
  <h2>BTC/ETH Bets with Technical Context</h2>
  <div style="overflow-x:auto"><table>
    <thead><tr><th>Placed (CT)</th><th>Sym</th><th>Result</th><th>Side</th>
    <th class="num">Price</th><th class="num">EMA-21</th><th class="num">RSI</th>
    <th>Trend</th><th class="num">P&L</th></tr></thead>
    <tbody>{bet_rows}</tbody>
  </table></div>
</div>

<script>
const GRID='rgba(255,255,255,0.07)',TICK='#64748b';

// RSI win rate
new Chart(document.getElementById('rsiChart'),{{
  type:'bar',
  data:{{
    labels:{json.dumps(rsi_labels)},
    datasets:[{{
      label:'Win %',
      data:{json.dumps(rsi_wps)},
      backgroundColor:{json.dumps(rsi_wps)}.map(v=>v>=70?'rgba(34,197,94,0.7)':v<=35?'rgba(239,68,68,0.7)':'rgba(245,158,11,0.7)'),
      borderRadius:4,
    }}]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}}}},
    scales:{{
      x:{{grid:{{color:GRID}},ticks:{{color:TICK,font:{{size:10}}}}}},
      y:{{grid:{{color:GRID}},ticks:{{color:TICK,callback:v=>v+'%'}},min:0,max:100}}
    }}
  }}
}});

// Hour chart
new Chart(document.getElementById('hourChart'),{{
  type:'bar',
  data:{{
    labels:{json.dumps([f"{h:02d}:00" for h in hour_labels])},
    datasets:[
      {{label:'P&L $',data:{json.dumps(hour_pnls)},backgroundColor:{json.dumps(hour_pnls)}.map(v=>v>=0?'rgba(34,197,94,0.6)':'rgba(239,68,68,0.6)'),borderRadius:3,yAxisID:'y'}},
      {{label:'Win %',data:{json.dumps(hour_wps)},type:'line',borderColor:'#3b82f6',pointRadius:4,tension:0.3,yAxisID:'y2'}},
    ]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{labels:{{color:TICK,font:{{size:10}}}}}}}},
    scales:{{
      x:{{grid:{{color:GRID}},ticks:{{color:TICK}}}},
      y:{{grid:{{color:GRID}},ticks:{{color:TICK,callback:v=>'$'+v}},position:'left'}},
      y2:{{grid:{{display:false}},ticks:{{color:TICK,callback:v=>v+'%'}},position:'right',min:0,max:100}}
    }}
  }}
}});
</script></body></html>"""


def build_insights(d):
    signal = d["signal_fn"]
    insights = []

    # RSI insights
    for label, s in d["rsi_stats"].items():
        wp, n = s["win_pct"], s["bets"]
        if wp >= 70 and n >= 2:
            insights.append(f'<div class="insight">✅ <strong>BET when RSI is "{label}"</strong> — '
                            f'{s["wins"]}/{n} wins ({wp:.0f}%), avg P&L ${s["avg_pnl"]:+.2f}</div>')
        elif wp <= 35 and n >= 2:
            insights.append(f'<div class="insight">❌ <strong>SIT OUT when RSI is "{label}"</strong> — '
                            f'only {s["wins"]}/{n} wins ({wp:.0f}%), avg P&L ${s["avg_pnl"]:+.2f}</div>')

    # Neutral zone warning
    if "neutral (45–55)" in d["rsi_stats"]:
        s = d["rsi_stats"]["neutral (45–55)"]
        if s["win_pct"] <= 40:
            insights.append(f'<div class="insight">⚠️  <strong>Avoid neutral RSI (45–55)</strong> — '
                            f'no directional edge, {s["wins"]}/{s["bets"]} wins ({s["win_pct"]:.0f}%)</div>')

    # Trend insights
    for k, s in d["trend_stats"].items():
        if k == "unknown": continue
        wp, n = s["win_pct"], s["bets"]
        if wp >= 65 and n >= 3:
            insights.append(f'<div class="insight">✅ <strong>Bet when price is {k} (above EMA-21)</strong> — '
                            f'{s["wins"]}/{n} wins ({wp:.0f}%)</div>')
        elif wp <= 35 and n >= 3:
            insights.append(f'<div class="insight">❌ <strong>Avoid when price is {k} (below EMA-21)</strong> — '
                            f'only {s["wins"]}/{n} wins ({wp:.0f}%)</div>')

    # Hour insights
    best_hours  = [(h, s) for h, s in d["hour_stats"].items() if s["win_pct"] >= 70 and s["bets"] >= 2]
    worst_hours = [(h, s) for h, s in d["hour_stats"].items() if s["win_pct"] <= 35 and s["bets"] >= 2]
    if best_hours:
        hrs = ", ".join(f"{h:02d}:00" for h, _ in sorted(best_hours))
        insights.append(f'<div class="insight">⏰ <strong>Best hours to bet (CT): {hrs}</strong> — '
                        f'high win rates historically</div>')
    if worst_hours:
        hrs = ", ".join(f"{h:02d}:00" for h, _ in sorted(worst_hours))
        insights.append(f'<div class="insight">🚫 <strong>Avoid betting at (CT): {hrs}</strong> — '
                        f'consistently losing hours</div>')

    if not insights:
        insights.append('<div class="insight">⚠️  <strong>Not enough data yet</strong> for high-confidence signals. '
                        'Keep tracking — patterns will emerge with more bets.</div>')
    return insights


def main():
    data = build_analysis()
    html = build_html(data)
    out  = os.path.join(tempfile.gettempdir(), "kalshi_analysis.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"Saved to {out}")
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
