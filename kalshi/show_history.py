"""
Render all settled bets and open positions from Kalshi.
Run with: op run --env-file=.env -- python3 -m kalshi.show_history
"""
from collections import defaultdict
from kalshi import KalshiClient
from kalshi.utils import paginate, parse_settlement, norm_dt, CT

SEP = "=" * 112
DIV = "-" * 112


def main():
    c = KalshiClient()
    balance = c.get_balance()
    base = "https://api.elections.kalshi.com/trade-api/v2"

    settlements = paginate(c.session, f"{base}/portfolio/settlements", "settlements")
    fills_raw   = paginate(c.session, f"{base}/portfolio/fills", "fills")
    positions   = c.get_positions()

    fills_by_ticker = defaultdict(list)
    for f in fills_raw:
        fills_by_ticker[f["market_ticker"]].append(f)

    print(f"Balance: ${balance / 100:.2f}\n")

    print(SEP)
    print(f" SETTLED BETS  ({len(settlements)} total)")
    print(SEP)
    print(
        f"{'Placed':<12} {'Settled':<19} {'Ticker':<38} {'Res':<5} "
        f"{'Side':<4} {'YES Cts':>8} {'NO Cts':>7} {'$ Cost':>8} {'Payout':>8} {'P&L':>9}"
    )
    print(DIV)

    total_pnl = 0.0
    wins = 0

    for s in sorted(settlements, key=lambda x: x["settled_time"], reverse=True):
        p = parse_settlement(s)

        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if ticker_fills:
            earliest  = min(ticker_fills, key=lambda f: f["created_time"])
            placed_str = norm_dt(earliest["created_time"]).astimezone(CT).strftime("%H:%M CT")
        else:
            placed_str = "—"

        total_pnl += p["pnl"]
        if p["won"]:
            wins += 1

        result_tag = "WIN" if p["won"] else "LOSS"
        pnl_str = f"+${p['pnl']:.2f}" if p["pnl"] >= 0 else f"-${abs(p['pnl']):.2f}"
        yes_str = f"{p['yes_qty']:.2f}" if p["yes_qty"] > 0 else "—"
        no_str  = f"{p['no_qty']:.2f}"  if p["no_qty"]  > 0 else "—"

        print(
            f"{placed_str:<12} {p['settled_str']:<19} {p['ticker']:<38} {result_tag:<5} "
            f"{p['side']:<4} {yes_str:>8} {no_str:>7} ${p['total_cost']:>6.2f} ${p['payout']:>6.2f} {pnl_str:>9}"
        )

    print(DIV)
    total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    win_rate = wins / len(settlements) * 100 if settlements else 0
    print(f"  {wins}W / {len(settlements) - wins}L  ({win_rate:.0f}% win rate)   Total P&L: {total_str}")

    # --- Time-of-day breakdown ---
    print()
    print(SEP)
    print(" BETS BY HOUR OF DAY  (CT)")
    print(SEP)
    hour_stats = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for s in settlements:
        ticker_fills = fills_by_ticker.get(s["ticker"], [])
        if not ticker_fills:
            continue
        earliest = min(ticker_fills, key=lambda f: f["created_time"])
        hour = norm_dt(earliest["created_time"]).astimezone(CT).hour
        p = parse_settlement(s)
        hour_stats[hour]["bets"] += 1
        hour_stats[hour]["wins"] += int(p["won"])
        hour_stats[hour]["pnl"]  += p["pnl"]

    print(f"  {'Hour (CT)':<14} {'Bets':>5} {'Wins':>5} {'Win%':>6} {'P&L':>9}")
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
