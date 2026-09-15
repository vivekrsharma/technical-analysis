"""
Shared utilities for Kalshi data parsing.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def norm_dt(s: str) -> datetime:
    """Parse Kalshi timestamp string to timezone-aware datetime."""
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_settlement(s: dict) -> dict:
    """
    Parse a Kalshi settlement record into clean P&L fields.

    Handles:
    - revenue=0 bug for recent settlements (compute payout from contract counts)
    - Positions holding both YES and NO sides (total cost = yes_cost + no_cost)
    """
    yes_qty  = float(s.get("yes_count_fp", 0))
    no_qty   = float(s.get("no_count_fp", 0))
    result   = s.get("market_result", "")
    yes_cost = float(s.get("yes_total_cost_dollars", 0))
    no_cost  = float(s.get("no_total_cost_dollars", 0))

    # Always sum both sides — user may hold YES and NO simultaneously
    total_cost = yes_cost + no_cost

    # Payout = winning contracts × $1 per contract
    # Trust revenue field only when it's non-zero
    raw_revenue = s.get("revenue", 0) / 100
    if raw_revenue > 0:
        payout = raw_revenue
    else:
        payout = yes_qty if result == "yes" else no_qty

    pnl  = round(payout - total_cost, 2)
    won  = payout > 0 and (
        (result == "yes" and yes_qty > 0) or
        (result == "no"  and no_qty  > 0)
    )

    # Primary side = whichever had the larger cost
    if yes_cost >= no_cost:
        side = "YES"
    else:
        side = "NO"

    settled_ct = norm_dt(s["settled_time"]).astimezone(CT)

    return {
        "ticker":      s["ticker"],
        "result":      result,
        "side":        side,
        "yes_qty":     round(yes_qty, 2),
        "no_qty":      round(no_qty, 2),
        "yes_cost":    round(yes_cost, 2),
        "no_cost":     round(no_cost, 2),
        "total_cost":  round(total_cost, 2),
        "payout":      round(payout, 2),
        "pnl":         pnl,
        "won":         won,
        "settled_ts":  settled_ct,
        "settled_str": settled_ct.strftime("%Y-%m-%d %H:%M CT"),
    }


def paginate(session, url: str, key: str, limit: int = 100) -> list:
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
