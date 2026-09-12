import os
import requests
from typing import Optional, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .models import Market, OrderBook, Order, Position, Trade, Event

BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"


class KalshiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


class KalshiClient:
    """
    Client for the Kalshi REST API v2.

    Authenticate with either an API key or email/password:
        client = KalshiClient(api_key="your-key")
        client = KalshiClient(email="you@example.com", password="secret")

    Credentials are read from env vars if not passed directly:
        KALSHI_API_KEY, KALSHI_EMAIL, KALSHI_PASSWORD
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        base_url: str = BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        api_key = api_key or os.getenv("KALSHI_API_KEY")
        email = email or os.getenv("KALSHI_EMAIL")
        password = password or os.getenv("KALSHI_PASSWORD")

        if api_key:
            self.session.headers["Authorization"] = api_key
        elif email and password:
            self._login(email, password)
        else:
            raise ValueError(
                "Provide api_key or email+password, "
                "or set KALSHI_API_KEY / KALSHI_EMAIL + KALSHI_PASSWORD env vars."
            )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/login",
            json={"email": email, "password": password},
        )
        self._raise_for_status(resp)
        token = resp.json()["token"]
        self.session.headers["Authorization"] = token

    def logout(self) -> None:
        self._post("/logout")

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_markets(
        self,
        status: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        tickers: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Market]:
        """Return markets. status: 'open' | 'closed' | 'settled'"""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)

        markets: List[Market] = []
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            markets.extend(Market.from_dict(m) for m in data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("markets", [])) < limit:
                break

        return markets

    def get_market(self, ticker: str) -> Market:
        data = self._get(f"/markets/{ticker}")
        return Market.from_dict(data["market"])

    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        return OrderBook.from_dict(ticker, data)

    def get_trades(
        self,
        ticker: str,
        limit: int = 100,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
    ) -> List[Trade]:
        params: dict = {"limit": limit}
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts

        trades: List[Trade] = []
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"/markets/{ticker}/trades", params=params)
            trades.extend(Trade.from_dict(ticker, t) for t in data.get("trades", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("trades", [])) < limit:
                break

        return trades

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(
        self,
        status: Optional[str] = None,
        series_ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Event]:
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker

        events: List[Event] = []
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/events", params=params)
            events.extend(Event.from_dict(e) for e in data.get("events", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("events", [])) < limit:
                break

        return events

    def get_event(self, event_ticker: str) -> Event:
        data = self._get(f"/events/{event_ticker}")
        return Event.from_dict(data["event"])

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def get_balance(self) -> int:
        """Returns available balance in cents."""
        data = self._get("/portfolio/balance")
        return data.get("balance", 0)

    def get_positions(
        self,
        ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Position]:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker

        positions: List[Position] = []
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/portfolio/positions", params=params)
            positions.extend(Position.from_dict(p) for p in data.get("market_positions", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("market_positions", [])) < limit:
                break

        return positions

    def get_orders(
        self,
        ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Order]:
        """status: 'resting' | 'filled' | 'canceled'"""
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status

        orders: List[Order] = []
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get("/portfolio/orders", params=params)
            orders.extend(Order.from_dict(o) for o in data.get("orders", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("orders", [])) < limit:
                break

        return orders

    def get_order(self, order_id: str) -> Order:
        data = self._get(f"/portfolio/orders/{order_id}")
        return Order.from_dict(data["order"])

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def create_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """
        Place an order.

        action:     'buy' | 'sell'
        side:       'yes' | 'no'
        order_type: 'limit' | 'market'
        yes_price / no_price: price in cents (1–99)
        """
        body: dict = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id

        data = self._post("/portfolio/orders", json=body)
        return Order.from_dict(data["order"])

    def cancel_order(self, order_id: str) -> Order:
        data = self._delete(f"/portfolio/orders/{order_id}")
        return Order.from_dict(data["order"])

    def cancel_all_orders(self, ticker: Optional[str] = None) -> List[Order]:
        """Cancel all resting orders, optionally filtered by ticker."""
        open_orders = self.get_orders(ticker=ticker, status="resting")
        canceled = []
        for order in open_orders:
            canceled.append(self.cancel_order(order.order_id))
        return canceled

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", params=params)
        self._raise_for_status(resp)
        return resp.json()

    def _post(self, path: str, json: Optional[dict] = None) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=json or {})
        self._raise_for_status(resp)
        return resp.json()

    def _delete(self, path: str) -> dict:
        resp = self.session.delete(f"{self.base_url}{path}")
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if not resp.ok:
            try:
                msg = resp.json().get("message", resp.text)
            except Exception:
                msg = resp.text
            raise KalshiError(resp.status_code, msg)
