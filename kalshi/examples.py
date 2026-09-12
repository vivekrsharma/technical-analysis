"""
Quick usage examples — run with: python -m kalshi.examples
Requires KALSHI_API_KEY or KALSHI_EMAIL + KALSHI_PASSWORD in environment.
"""
from kalshi import KalshiClient


def main():
    client = KalshiClient()

    # --- Market data ---
    print("=== Open markets (first 5) ===")
    markets = client.get_markets(status="open", limit=5)
    for m in markets:
        print(f"  {m.ticker:40s}  yes_ask={m.yes_ask}¢  vol={m.volume}")

    print("\n=== Balance ===")
    balance = client.get_balance()
    print(f"  ${balance / 100:.2f}")

    print("\n=== Positions ===")
    positions = client.get_positions()
    if positions:
        for p in positions:
            pnl = (p.realized_pnl + p.unrealized_pnl) / 100
            print(f"  {p.ticker:40s}  pos={p.position:+d}  P&L=${pnl:+.2f}")
    else:
        print("  (no open positions)")

    print("\n=== Resting orders ===")
    orders = client.get_orders(status="resting")
    if orders:
        for o in orders:
            print(f"  {o.order_id}  {o.ticker}  {o.action} {o.side}  qty={o.quantity}  price={o.price}¢")
    else:
        print("  (no open orders)")


if __name__ == "__main__":
    main()
