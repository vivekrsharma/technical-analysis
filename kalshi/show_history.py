"""
Render all settled bets and open positions from Kalshi.
Run with: op run --env-file=.env -- python3 -m kalshi.show_history
"""
import re
from collections import defaultdict
from datetime import datetime
from kalshi import KalshiClient

SEP = "=" * 110
DIV = "-" * 110


def fmt_dt(s):
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_time(s):
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.strftime("%H:%M")


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


def main():
    c = KalshiClient()
    balance = c.get_balance()
    base = "https://api.elections.kalshi.com/trade-api/v2"

    settlements = paginate(c.session, f"{base}/portfolio/settlements", "settlements")
    fills_raw   = paginate(c.session, f"{base}/portfolio/fills", "fills")
    positions   = c.get_positions()

    # Index fills by market ticker: ticker -> list of fills
    fills_by_ticker = defaultdict(list)
    for f in fills_raw:
        fills_by_ticker[f["market_ticker"]].append(f)

    print(f"Balance: ${balance / 100:.2f}\n")

    # --- Settled bets ---
    print(SEP)
    print(f" SETTLED BETS  ({len(settlements)} total)")
    print(SEP)
    print(
        f"{'Placed':<12} {'Settled':<17} {'Ticker':<38} {'Res':<5} "
        f"{'Side':<4} {'Contracts':>9} {'$ Placed':>9} {'Payout':>8} {'P&L':>9}"
    )
    print(DIV)

    total_pnl = 0.0
    wins = 0

    for s in sorted(settlements, key=lambda x: x["settled_time"], reverse=True):
        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty  = float(s.get("no_count_fp", 0))
        result  = s.get("market_result", "")
        revenue = s.get("revenue", 0) / 100

        if yes_qty > 0:
            side, qty, cost = "YES", yes_qty, float(s.get("yes_total_cost_dollars", 0))
        else:
            side, qty, cost = "NO", no_qty, float(s.get("no_total_cost_dollars", 0))

        won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        pnl = revenue - cost if won else -cost
        total_pnl += pnl
        if won:
            wins += 1

        # Derive placement time and total volume from fills
        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if ticker_fills:
            earliest = min(ticker_fills, key=lambda f: f["created_time"])
            placed_str = fmt_time(earliest["created_time"])
            volume = sum(float(f["count_fp"]) for f in ticker_fills)
        else:
            placed_str = "—"
            volume = qty

        result_tag = "WIN" if won else "LOSS"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        print(
            f"{placed_str:<12} {fmt_dt(s['settled_time']):<17} {s['ticker']:<38} {result_tag:<5} "
            f"{side:<4} {volume:>9.2f} ${cost:>7.2f} ${revenue:>6.2f} {pnl_str:>9}"
        )

    print(DIV)
    total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    win_rate = wins / len(settlements) * 100 if settlements else 0
    print(f"  {wins}W / {len(settlements) - wins}L  ({win_rate:.0f}% win rate)   Total P&L: {total_str}")

    # --- Time-of-day breakdown ---
    print()
    print(SEP)
    print(" BETS BY HOUR OF DAY  (UTC)")
    print(SEP)
    hour_stats = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for s in settlements:
        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if not ticker_fills:
            continue
        earliest = min(ticker_fills, key=lambda f: f["created_time"])
        ts = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], earliest["created_time"])
        hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour

        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty  = float(s.get("no_count_fp", 0))
        result  = s.get("market_result", "")
        revenue = s.get("revenue", 0) / 100
        side    = "YES" if yes_qty > 0 else "NO"
        cost    = float(s.get("yes_total_cost_dollars", 0) if yes_qty > 0 else s.get("no_total_cost_dollars", 0))
        won     = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        pnl     = revenue - cost if won else -cost

        hour_stats[hour]["bets"] += 1
        hour_stats[hour]["wins"] += int(won)
        hour_stats[hour]["pnl"]  += pnl

    print(f"  {'Hour (UTC)':<14} {'Bets':>5} {'Wins':>5} {'Win%':>6} {'P&L':>9}")
    print(f"  {'-'*44}")
    for hour in sorted(hour_stats):
        st = hour_stats[hour]
        wr = st["wins"] / st["bets"] * 100 if st["bets"] else 0
        pnl_str = f"+${st['pnl']:.2f}" if st["pnl"] >= 0 else f"-${abs(st['pnl']):.2f}"
        print(f"  {f'{hour:02d}:00–{hour:02d}:59':<14} {st['bets']:>5} {st['wins']:>5} {wr:>5.0f}% {pnl_str:>9}")

    # --- Open positions ---
    if positions:
        print()
        print(SEP)
        print(f" OPEN POSITIONS  ({len(positions)})")
        print(SEP)
        print(f"{'Ticker':<44} {'Pos':>5} {'Cost':>9} {'Unrealized P&L':>16} {'Realized P&L':>14}")
        print(DIV)
        for p in positions:
            upnl = f"+${p.unrealized_pnl/100:.2f}" if p.unrealized_pnl >= 0 else f"-${abs(p.unrealized_pnl)/100:.2f}"
            rpnl = f"+${p.realized_pnl/100:.2f}" if p.realized_pnl >= 0 else f"-${abs(p.realized_pnl)/100:.2f}"
            print(f"{p.ticker:<44} {p.position:>5} ${p.total_cost/100:>7.2f} {upnl:>16} {rpnl:>14}")
    else:
        print("\n  No open positions.")


if __name__ == "__main__":
    main()
