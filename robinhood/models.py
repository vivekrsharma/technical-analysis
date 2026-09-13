from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class CryptoPosition:
    symbol: str
    name: str
    quantity: float
    average_buy_price: float
    cost_basis: float
    current_price: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass
class CryptoOrder:
    order_id: str
    symbol: str
    side: str               # buy / sell
    order_type: str         # market / limit
    state: str              # filled / canceled / partially_filled / pending
    quantity: float
    filled_quantity: float
    price: Optional[float]  # None for market orders
    average_price: Optional[float]
    total_notional: float   # total $ value of the fill
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


@dataclass
class CryptoQuote:
    symbol: str
    bid_price: float
    ask_price: float
    mark_price: float
    high_price: float
    low_price: float
    open_price: float
    volume: float

    @property
    def spread(self) -> float:
        return round(self.ask_price - self.bid_price, 6)
