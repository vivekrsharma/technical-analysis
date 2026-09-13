import os
from datetime import datetime
from typing import Optional

import pyotp
import robin_stocks.robinhood as rh

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .models import CryptoPosition, CryptoOrder, CryptoQuote


class RobinhoodClient:
    """
    Robinhood crypto client built on robin_stocks.

    Credentials are read from env vars (use op run --env-file=.env --):
        ROBINHOOD_USERNAME    — account email
        ROBINHOOD_PASSWORD    — account password
        ROBINHOOD_MFA_SECRET  — TOTP secret from your authenticator app

    On first login, robin_stocks caches the session token at
    ~/.tokens/robinhood.pickle so subsequent calls skip MFA entirely.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        mfa_secret: Optional[str] = None,
    ):
        username   = username   or os.getenv("ROBINHOOD_USERNAME")
        password   = password   or os.getenv("ROBINHOOD_PASSWORD")
        mfa_secret = mfa_secret or os.getenv("ROBINHOOD_MFA_SECRET")

        if not username or not password:
            raise ValueError(
                "Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD env vars."
            )

        raw_code = os.getenv("ROBINHOOD_MFA_CODE")
        if raw_code:
            mfa_code = raw_code.strip()
        elif mfa_secret:
            mfa_code = pyotp.TOTP(mfa_secret).now()
        else:
            mfa_code = None

        rh.login(
            username=username,
            password=password,
            mfa_code=mfa_code,
            store_session=True,
        )

        # Build UUID → symbol map from Robinhood's currency pairs endpoint
        self._pair_map: dict[str, str] = {}
        try:
            pairs = rh.crypto.get_crypto_currency_pairs() or []
            for pair in pairs:
                uid    = pair.get("id", "")
                symbol = pair.get("asset_currency", {}).get("code", "")
                if uid and symbol:
                    self._pair_map[uid] = symbol
        except Exception:
            pass

    def _resolve_symbol(self, pair_id: str) -> str:
        """Resolve a currency pair UUID or 'BTC-USD' string to a ticker symbol."""
        if pair_id in self._pair_map:
            return self._pair_map[pair_id]
        if "-" in pair_id and len(pair_id) < 20:
            return pair_id.split("-")[0]
        return pair_id

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[CryptoPosition]:
        """Return all current crypto holdings with live P&L."""
        raw = rh.crypto.get_crypto_positions() or []
        positions = []
        for p in raw:
            qty = float(p.get("quantity", 0))
            if qty < 1e-8:
                continue

            symbol = p["currency"]["code"]
            name   = p["currency"]["name"]

            cost_bases = p.get("cost_bases", [{}])
            cost_basis = float(cost_bases[0].get("direct_cost_basis", 0)) if cost_bases else 0.0
            avg_buy    = cost_basis / qty if qty else 0

            quote         = rh.crypto.get_crypto_quote(symbol)
            current_price = float(quote.get("mark_price", 0)) if quote else 0
            current_value = current_price * qty
            unreal_pnl    = current_value - cost_basis
            unreal_pct    = (unreal_pnl / cost_basis * 100) if cost_basis else 0

            positions.append(CryptoPosition(
                symbol=symbol,
                name=name,
                quantity=qty,
                average_buy_price=round(avg_buy, 6),
                cost_basis=round(cost_basis, 2),
                current_price=round(current_price, 6),
                current_value=round(current_value, 2),
                unrealized_pnl=round(unreal_pnl, 2),
                unrealized_pnl_pct=round(unreal_pct, 2),
            ))

        return positions

    # ------------------------------------------------------------------
    # Order history
    # ------------------------------------------------------------------

    def get_orders(
        self,
        symbol: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[CryptoOrder]:
        """
        Return crypto order history.

        symbol: e.g. 'BTC', 'ETH' — filters by currency pair
        state:  'filled' | 'canceled' | 'partially_filled' | 'pending'
        """
        raw = rh.orders.get_all_crypto_orders() or []
        orders = []
        for o in raw:
            sym = self._resolve_symbol(o.get("currency_pair_id", ""))
            if symbol and sym.upper() != symbol.upper():
                continue
            if state and o.get("state") != state:
                continue

            orders.append(CryptoOrder(
                order_id=o.get("id", ""),
                symbol=sym,
                side=o.get("side", ""),
                order_type=o.get("type", ""),
                state=o.get("state", ""),
                quantity=float(o.get("quantity", 0)),
                filled_quantity=float(o.get("cumulative_quantity", 0)),
                price=_optional_float(o.get("price")),
                average_price=_optional_float(o.get("average_price")),
                total_notional=float(o.get("rounded_executed_notional", 0) or 0),
                created_at=_parse_dt(o.get("created_at")),
                updated_at=_parse_dt(o.get("updated_at")),
            ))

        return orders

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> CryptoQuote:
        """Live quote for a single symbol (e.g. 'BTC')."""
        q = rh.crypto.get_crypto_quote(symbol)
        if not q:
            raise ValueError(f"No quote returned for {symbol}")
        return CryptoQuote(
            symbol=symbol.upper(),
            bid_price=float(q.get("bid_price", 0)),
            ask_price=float(q.get("ask_price", 0)),
            mark_price=float(q.get("mark_price", 0)),
            high_price=float(q.get("high_price", 0)),
            low_price=float(q.get("low_price", 0)),
            open_price=float(q.get("open_price", 0)),
            volume=float(q.get("volume", 0)),
        )

    def get_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        return [self.get_quote(s) for s in symbols]

    # ------------------------------------------------------------------
    # P&L summary
    # ------------------------------------------------------------------

    def get_pnl_summary(self) -> dict:
        """Aggregate realized P&L from order history and unrealized from positions."""
        positions = self.get_positions()
        orders    = self.get_orders(state="filled")

        realized: dict[str, dict] = {}
        for o in orders:
            sym = o.symbol
            if sym not in realized:
                realized[sym] = {"buy_cost": 0.0, "sell_proceeds": 0.0}
            if o.side == "buy":
                realized[sym]["buy_cost"] += o.total_notional
            else:
                realized[sym]["sell_proceeds"] += o.total_notional

        summary: dict[str, dict] = {}
        for sym, vals in realized.items():
            summary[sym] = {
                "realized_pnl": round(vals["sell_proceeds"] - vals["buy_cost"], 2),
                "total_bought": round(vals["buy_cost"], 2),
                "total_sold":   round(vals["sell_proceeds"], 2),
                "unrealized_pnl":     0.0,
                "unrealized_pnl_pct": 0.0,
                "current_value":      0.0,
            }

        for p in positions:
            if p.symbol not in summary:
                summary[p.symbol] = {
                    "realized_pnl": 0.0,
                    "total_bought": 0.0,
                    "total_sold":   0.0,
                }
            summary[p.symbol]["unrealized_pnl"]     = p.unrealized_pnl
            summary[p.symbol]["unrealized_pnl_pct"] = p.unrealized_pnl_pct
            summary[p.symbol]["current_value"]       = p.current_value

        return summary

    def logout(self) -> None:
        rh.logout()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _optional_float(val) -> Optional[float]:
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
