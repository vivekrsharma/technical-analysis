"""
Render all settled bets and open positions from Kalshi.
Run with: op run --env-file=.env -- python3 -m kalshi.show_history
"""
import re
from datetime import datetime
from kalshi import KalshiClient

SEP = "=" * 92
DIV = "-" * 92


def fmt_dt(s):
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def main():
    c = KalshiClient()
    balance = c.get_balance()

    # --- Paginate all settlements ---
    settlements = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = c.session.get(
            "https://api.elections.kalshi.com/trade-api/v2/portfolio/settlements",
            params=params,
        )
        data = resp.json()
        batch = data.get("settlements", [])
        settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or len(batch) < 100:
            break

    positions = c.get_positions()

    print(f"Balance: ${balance / 100:.2f}\n")

    # --- Settled bets ---
    print(SEP)
    print(f" SETTLED BETS  ({len(settlements)} total)")
    print(SEP)
    header = f"{'Date':<18} {'Ticker':<40} {'Result':<7} {'Side':<5} {'Qty':>5} {'Cost':>8} {'Payout':>8} {'P&L':>9}"
    print(header)
    print(DIV)

    total_pnl = 0.0
    wins = 0

    for s in sorted(settlements, key=lambda x: x["settled_time"], reverse=True):
        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty = float(s.get("no_count_fp", 0))
        result = s.get("market_result", "")
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

        result_tag = "WIN " if won else "LOSS"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        print(
            f"{fmt_dt(s['settled_time']):<18} {s['ticker']:<40} {result_tag:<7} "
            f"{side:<5} {qty:>5.1f} ${cost:>6.2f} ${revenue:>6.2f} {pnl_str:>9}"
        )

    print(DIV)
    total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    win_rate = wins / len(settlements) * 100 if settlements else 0
    print(f"  {wins}W / {len(settlements) - wins}L  ({win_rate:.0f}% win rate)   Total P&L: {total_str}")

    # --- Open positions ---
    if positions:
        print()
        print(SEP)
        print(f" OPEN POSITIONS  ({len(positions)})")
        print(SEP)
        pos_header = f"{'Ticker':<44} {'Pos':>5} {'Cost':>9} {'Unrealized P&L':>16} {'Realized P&L':>14}"
        print(pos_header)
        print(DIV)
        for p in positions:
            upnl = f"+${p.unrealized_pnl/100:.2f}" if p.unrealized_pnl >= 0 else f"-${abs(p.unrealized_pnl)/100:.2f}"
            rpnl = f"+${p.realized_pnl/100:.2f}" if p.realized_pnl >= 0 else f"-${abs(p.realized_pnl)/100:.2f}"
            print(f"{p.ticker:<44} {p.position:>5} ${p.total_cost/100:>7.2f} {upnl:>16} {rpnl:>14}")
    else:
        print("\n  No open positions.")


if __name__ == "__main__":
    main()
