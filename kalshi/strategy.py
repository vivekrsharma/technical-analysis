"""
Real-time Kalshi betting checklist based on your historical edge patterns.

Run with: op run --env-file=.env -- python3 -m kalshi.strategy
Add --html flag to also open a browser view.
"""
import os
import re
import sys
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import robin_stocks.robinhood as rh

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CT = ZoneInfo("America/Chicago")

# ─── Rules derived from historical analysis ──────────────────────────────────
# RSI win rates:  30-45 → 62% (+$1.47/bet)  |  45-55 → 42% (-$1.49/bet)
# Worst hour CT:  17:00 → 12% win rate
# Best hours CT:  23:00, 16:00, 19:00 → 100% win rate (small sample, use with caution)
# Trend:          bearish (price < EMA21) → 54%  |  bullish → 38%

AVOID_HOURS_CT   = {17}                    # 5 PM CT — consistently losing
STRONG_HOURS_CT  = {23, 16, 19}            # historically best (small n)
RSI_EDGE_MAX     = 45                      # RSI below this = edge
RSI_NOEDGE_MIN   = 55                      # RSI above this = watch
RSI_DANGER_MAX   = 30                      # RSI below this = deep oversold


# ─── helpers ─────────────────────────────────────────────────────────────────

def parse_dt(s):
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    rsi = [None] * (period + 1)
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al else float("inf")
        rsi.append(round(100 - 100 / (1 + rs), 1))
    return rsi


def compute_ema(closes, period):
    if len(closes) < period:
        return [None] * len(closes)
    k   = 2 / (period + 1)
    ema = [None] * (period - 1) + [sum(closes[:period]) / period]
    for i in range(period, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    return ema


def get_indicators(symbol):
    raw = rh.crypto.get_crypto_historicals(symbol, interval="hour", span="month") or []
    if not raw:
        return None
    closes = [float(c["close_price"]) for c in raw]
    rsi14  = compute_rsi(closes, 14)
    ema8   = compute_ema(closes, 8)
    ema21  = compute_ema(closes, 21)
    ema50  = compute_ema(closes, 50)

    # Current (most recent complete candle)
    idx    = -1
    price  = closes[idx]
    rsi    = rsi14[idx]
    e8     = ema8[idx]
    e21    = ema21[idx]
    e50    = ema50[idx]
    trend  = "bullish" if (e21 and price > e21) else "bearish"
    align  = "bullish" if (e8 and e21 and e8 > e21) else "bearish"

    # RSI direction (last 3 candles)
    rsi_series = [r for r in rsi14[-5:] if r is not None]
    rsi_dir    = "rising" if (len(rsi_series) >= 2 and rsi_series[-1] > rsi_series[-2]) else "falling"

    # Price momentum (last 3 candle closes)
    momentum_pct = round((closes[-1] - closes[-4]) / closes[-4] * 100, 2) if len(closes) >= 4 else 0

    return {
        "symbol":       symbol,
        "price":        price,
        "rsi":          rsi,
        "ema8":         e8,
        "ema21":        e21,
        "ema50":        e50,
        "trend":        trend,
        "align":        align,
        "rsi_dir":      rsi_dir,
        "momentum_pct": momentum_pct,
    }


# ─── checklist logic ─────────────────────────────────────────────────────────

CHECK  = "✅"
WARN   = "⚠️ "
FAIL   = "❌"
INFO   = "ℹ️ "

def evaluate(ind, now_ct):
    hour   = now_ct.hour
    rsi    = ind["rsi"]
    trend  = ind["trend"]
    align  = ind["align"]
    sym    = ind["symbol"]
    checks = []
    score  = 0   # positive = lean BET, negative = lean SIT OUT

    # --- RSI check ---
    if rsi is None:
        checks.append((WARN, "RSI", "not enough data"))
    elif rsi < RSI_DANGER_MAX:
        checks.append((CHECK, "RSI", f"{rsi:.1f} — deeply oversold, strong bounce candidate → lean YES"))
        score += 2
    elif rsi < RSI_EDGE_MAX:
        checks.append((CHECK, "RSI", f"{rsi:.1f} — in edge zone (30–45) → historical win rate 62%"))
        score += 1
    elif rsi < RSI_NOEDGE_MIN:
        checks.append((FAIL, "RSI", f"{rsi:.1f} — neutral zone (45–55), no directional edge → SIT OUT"))
        score -= 2
    else:
        checks.append((WARN, "RSI", f"{rsi:.1f} — elevated (>55), possible overbought → lean NO"))
        score += 0

    # --- Trend check (price vs EMA-21) ---
    if ind["ema21"]:
        pct_from_ema = round((ind["price"] - ind["ema21"]) / ind["ema21"] * 100, 2)
        if trend == "bearish":
            checks.append((CHECK, "Trend", f"price below EMA-21 ({pct_from_ema:+.1f}%) — bearish trend historically 54% win rate"))
            score += 1
        else:
            checks.append((WARN, "Trend", f"price above EMA-21 ({pct_from_ema:+.1f}%) — bullish trend historically only 38% → if betting, lean NO"))
            score -= 1

    # --- EMA alignment ---
    if ind["ema8"] and ind["ema21"]:
        if align == "bearish":
            checks.append((CHECK, "EMA align", "EMA-8 below EMA-21 — downtrend alignment"))
        else:
            checks.append((INFO,  "EMA align", "EMA-8 above EMA-21 — uptrend alignment"))

    # --- RSI direction ---
    checks.append((INFO, "RSI dir", f"RSI is {ind['rsi_dir']} over last 3 hours"))

    # --- Momentum ---
    mom = ind["momentum_pct"]
    if abs(mom) > 1.5:
        dir_word = "up" if mom > 0 else "down"
        checks.append((INFO, "Momentum", f"price {dir_word} {abs(mom):.1f}% over last 3 hours"))
    else:
        checks.append((INFO, "Momentum", f"price flat ({mom:+.1f}% over 3h) — low directional bias"))

    # --- Hour of day ---
    if hour in AVOID_HOURS_CT:
        checks.append((FAIL, "Hour", f"{hour:02d}:00 CT — historically your WORST hour (12% win rate) → DO NOT BET"))
        score -= 3
    elif hour in STRONG_HOURS_CT:
        checks.append((CHECK, "Hour", f"{hour:02d}:00 CT — historically strong hour (100% win rate, small sample)"))
        score += 1
    else:
        checks.append((INFO, "Hour", f"{hour:02d}:00 CT — neutral hour"))

    # --- Verdict ---
    if score >= 2:
        verdict = ("BET", "green", "Conditions align with your historical edge.")
    elif score <= -2:
        verdict = ("SIT OUT", "red", "Multiple signals against — preserve capital.")
    else:
        verdict = ("COIN FLIP", "yellow", "Mixed signals — bet small or skip.")

    # Lean direction
    if rsi and rsi > RSI_NOEDGE_MIN and trend == "bullish":
        lean = "if betting → lean NO (price extended)"
    elif rsi and rsi < RSI_EDGE_MAX and trend == "bearish":
        lean = "lean YES (oversold + bearish trend = bounce)"
    elif trend == "bearish":
        lean = "lean NO (price below EMA-21)"
    else:
        lean = "lean NO (bullish price action historically underperforms)"

    return checks, verdict, lean, score


# ─── terminal output ─────────────────────────────────────────────────────────

W = 62

def bar(char="═"): return char * W

def main():
    open_html = "--html" in sys.argv

    print("\nFetching live BTC & ETH data...")
    rh.login(
        username=os.environ["ROBINHOOD_USERNAME"],
        password=os.environ["ROBINHOOD_PASSWORD"],
        store_session=True,
    )

    btc = get_indicators("BTC")
    eth = get_indicators("ETH")
    now = datetime.now(CT)

    print()
    print(bar())
    print(f"  KALSHI BET CHECKLIST  —  {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(bar())

    for ind in [btc, eth]:
        if not ind:
            continue
        checks, verdict, lean, score = evaluate(ind, now)
        v_sym, v_color, v_note = verdict

        print(f"\n  {ind['symbol']}  ${ind['price']:>10,.2f}   RSI {ind['rsi']:.1f}   {ind['trend'].upper()}  ({ind['align']} EMA align)")
        print(f"  {'─'*56}")
        for icon, label, msg in checks:
            print(f"  {icon}  {label:<12}  {msg}")
        print(f"\n  {'─'*56}")
        color_block = {"green": "✅", "red": "❌", "yellow": "⚠️ "}[v_color]
        print(f"  {color_block}  SIGNAL: {v_sym}")
        print(f"        {v_note}")
        print(f"        {lean}")

    print()
    print(bar())
    print()

    if open_html:
        _open_html(btc, eth, now)


# ─── HTML output ─────────────────────────────────────────────────────────────

def _open_html(btc, eth, now):
    def card(ind):
        if not ind:
            return ""
        checks, verdict, lean, score = evaluate(ind, now)
        v_name, v_color, v_note = verdict
        badge_style = {
            "green":  "background:rgba(34,197,94,.15);color:#22c55e",
            "red":    "background:rgba(239,68,68,.15);color:#ef4444",
            "yellow": "background:rgba(245,158,11,.15);color:#f59e0b",
        }[v_color]

        rows = ""
        for icon, label, msg in checks:
            rows += f"<tr><td style='padding:6px 0;font-size:16px'>{icon}</td><td style='padding:6px 8px;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap'>{label}</td><td style='padding:6px 0;font-size:12px'>{msg}</td></tr>"

        rsi_color = "#22c55e" if (ind['rsi'] and ind['rsi'] < 45) else ("#ef4444" if (ind['rsi'] and ind['rsi'] > 55) else "#f59e0b")
        trend_color = "#ef4444" if ind['trend'] == "bearish" else "#22c55e"

        return f"""
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:12px;padding:24px;margin-bottom:20px">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">
    <div>
      <div style="font-size:22px;font-weight:700">{ind['symbol']}
        <span style="font-size:14px;color:#94a3b8;font-weight:400;margin-left:8px">${ind['price']:,.2f}</span>
      </div>
      <div style="margin-top:6px;font-size:12px">
        <span style="color:{rsi_color}">RSI {ind['rsi']:.1f}</span>
        &nbsp;·&nbsp;
        <span style="color:{trend_color}">{ind['trend'].upper()}</span>
        &nbsp;·&nbsp;
        <span style="color:#94a3b8">{ind['align']} EMA align</span>
        &nbsp;·&nbsp;
        <span style="color:#94a3b8">momentum {ind['momentum_pct']:+.1f}%</span>
      </div>
    </div>
    <div style="text-align:right">
      <span style="display:inline-block;padding:6px 16px;border-radius:6px;font-weight:700;font-size:15px;{badge_style}">{v_name}</span>
      <div style="font-size:11px;color:#94a3b8;margin-top:6px;max-width:200px">{lean}</div>
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse">{rows}</table>
  <div style="margin-top:16px;padding:10px 14px;background:rgba(255,255,255,.04);border-radius:6px;font-size:12px;color:#94a3b8">{v_note}</div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi Strategy Checklist</title>
<style>
  body{{background:#0f1117;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:32px;max-width:860px;margin:0 auto}}
  h1{{font-size:18px;font-weight:600;margin-bottom:4px}}
  .sub{{color:#94a3b8;font-size:12px;margin-bottom:28px}}
</style></head><body>
<h1>Kalshi Bet Checklist</h1>
<p class="sub">Live snapshot — {now.strftime('%Y-%m-%d %H:%M CT')} &nbsp;·&nbsp; Based on your historical edge patterns</p>
{card(btc)}
{card(eth)}
<div style="background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:18px;font-size:12px;color:#64748b;line-height:1.8">
  <strong style="color:#94a3b8">Rules derived from your 45-bet history:</strong><br>
  RSI 30–45 → 62% win rate (+$1.47 avg) &nbsp;|&nbsp;
  RSI 45–55 → 42% win rate (-$1.49 avg) &nbsp;|&nbsp;
  Bearish trend → 54% &nbsp;|&nbsp; Bullish trend → 38%<br>
  Worst hour: 17:00 CT (12%) &nbsp;|&nbsp; Best hours: 23:00, 16:00, 19:00 CT
</div>
</body></html>"""

    out = os.path.join(tempfile.gettempdir(), "kalshi_strategy.html")
    with open(out, "w") as f:
        f.write(html)
    subprocess.run(["open", out])


if __name__ == "__main__":
    main()
