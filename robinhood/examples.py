"""
Quick usage examples.
Run with: op run --env-file=.env -- python3 -m robinhood.examples
"""
from zoneinfo import ZoneInfo
from robinhood import RobinhoodClient

CT = ZoneInfo("America/Chicago")


def fmt_dt(dt):
    if not dt:
        return "—"
    return dt.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")


def main():
    c = RobinhoodClient()

    print("=== Crypto Positions ===")
    positions = c.get_positions()
    if positions:
        for p in positions:
            pnl = f"+${p.unrealized_pnl:.2f}" if p.unrealized_pnl >= 0 else f"-${abs(p.unrealized_pnl):.2f}"
            print(f"  {p.symbol:<6} {p.quantity:.6f} units  avg ${p.average_buy_price:.2f}  "
                  f"now ${p.current_price:.2f}  value ${p.current_value:.2f}  "
                  f"unrealized {pnl} ({p.unrealized_pnl_pct:+.1f}%)")
    else:
        print("  (no open positions)")

    print("\n=== Recent Orders (last 15 filled) ===")
    orders = c.get_orders(state="filled")[:15]
    for o in orders:
        print(f"  {fmt_dt(o.created_at)}  {o.symbol:<6} {o.side:<4}  "
              f"qty={o.filled_quantity:.6f}  avg=${o.average_price or 0:.2f}  total=${o.total_notional:.2f}")

    print("\n=== Live Quotes ===")
    syms = list({p.symbol for p in positions}) if positions else ["BTC", "ETH"]
    for q in c.get_quotes(syms):
        print(f"  {q.symbol:<6}  bid=${q.bid_price:.2f}  ask=${q.ask_price:.2f}  "
              f"mark=${q.mark_price:.2f}  spread=${q.spread:.4f}")

    print("\n=== P&L Summary ===")
    pnl = c.get_pnl_summary()
    for sym, d in sorted(pnl.items()):
        rpnl = d.get("realized_pnl", 0)
        upnl = d.get("unrealized_pnl", 0)
        val  = d.get("current_value", 0)
        print(f"  {sym:<6}  realized {'+' if rpnl>=0 else ''}{rpnl:.2f}  "
              f"unrealized {'+' if upnl>=0 else ''}{upnl:.2f}  "
              f"{'current value $'+str(round(val,2)) if val else ''}")


if __name__ == "__main__":
    main()
