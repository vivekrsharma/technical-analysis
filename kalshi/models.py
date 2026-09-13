import re
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Market:
    ticker: str
    title: str
    status: str
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    last_price: Optional[int]
    volume: int
    volume_24h: int
    open_interest: int
    result: Optional[str]
    close_time: Optional[datetime]

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        return cls(
            ticker=d["ticker"],
            title=d.get("title", ""),
            status=d.get("status", ""),
            yes_bid=d.get("yes_bid", 0),
            yes_ask=d.get("yes_ask", 0),
            no_bid=d.get("no_bid", 0),
            no_ask=d.get("no_ask", 0),
            last_price=d.get("last_price"),
            volume=d.get("volume", 0),
            volume_24h=d.get("volume_24h", 0),
            open_interest=d.get("open_interest", 0),
            result=d.get("result"),
            close_time=_parse_dt(d.get("close_time")),
        )


@dataclass
class OrderBookLevel:
    price: int
    quantity: int


@dataclass
class OrderBook:
    ticker: str
    yes: List[OrderBookLevel]
    no: List[OrderBookLevel]

    @classmethod
    def from_dict(cls, ticker: str, d: dict) -> "OrderBook":
        ob = d.get("orderbook", d)
        return cls(
            ticker=ticker,
            yes=[OrderBookLevel(price=lv[0], quantity=lv[1]) for lv in ob.get("yes", [])],
            no=[OrderBookLevel(price=lv[0], quantity=lv[1]) for lv in ob.get("no", [])],
        )


@dataclass
class Order:
    order_id: str
    ticker: str
    action: str
    side: str
    type: str
    quantity: int
    price: Optional[int]
    filled_quantity: int
    remaining_quantity: int
    status: str
    created_time: Optional[datetime]

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        qty = d.get("count", 0)
        filled = d.get("filled_count", 0)
        return cls(
            order_id=d["order_id"],
            ticker=d.get("ticker", ""),
            action=d.get("action", ""),
            side=d.get("side", ""),
            type=d.get("type", ""),
            quantity=qty,
            price=d.get("yes_price") or d.get("no_price"),
            filled_quantity=filled,
            remaining_quantity=qty - filled,
            status=d.get("status", ""),
            created_time=_parse_dt(d.get("created_time")),
        )


@dataclass
class Position:
    ticker: str
    position: int
    market_exposure: int
    realized_pnl: int
    unrealized_pnl: int
    total_cost: int

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d["ticker"],
            position=d.get("position", 0),
            market_exposure=d.get("market_exposure", 0),
            realized_pnl=d.get("realized_pnl", 0),
            unrealized_pnl=d.get("unrealized_pnl", 0),
            total_cost=d.get("total_cost", 0),
        )


@dataclass
class Trade:
    trade_id: str
    ticker: str
    yes_price: int
    count: int
    created_time: Optional[datetime]

    @classmethod
    def from_dict(cls, ticker: str, d: dict) -> "Trade":
        return cls(
            trade_id=d["trade_id"],
            ticker=ticker,
            yes_price=d.get("yes_price", 0),
            count=d.get("count", 0),
            created_time=_parse_dt(d.get("created_time")),
        )


@dataclass
class Event:
    event_ticker: str
    title: str
    status: str
    category: str
    markets: List[Market]

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            event_ticker=d["event_ticker"],
            title=d.get("title", ""),
            status=d.get("status", ""),
            category=d.get("category", ""),
            markets=[Market.from_dict(m) for m in d.get("markets", [])],
        )
