"""
Kalshi API client — wraps kalshi-python for market data + trading.

Prices on Kalshi are in CENTS (0-100), not fractions.
We keep them as cents internally and convert to dollars only for display.
"""
import logging
import os
import requests

log = logging.getLogger(__name__)

KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

# Authenticated kalshi-python client + sub-APIs (set by auth.init())
_client        = None
_portfolio_api = None
_markets_api   = None


# ── Public market data (no auth needed) ─────────────────────────────────────

def get_markets(limit=200, status="open", cursor=None):
    """Fetch open markets sorted by volume."""
    params = {"limit": limit, "status": status}
    if cursor:
        params["cursor"] = cursor
    try:
        resp = _session.get(f"{KALSHI_HOST}/markets", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("markets", []), data.get("cursor")
    except Exception as e:
        log.error(f"get_markets failed: {e}")
        return [], None


def get_market(ticker: str) -> dict:
    """Fetch a single market by ticker."""
    try:
        resp = _session.get(f"{KALSHI_HOST}/markets/{ticker}", timeout=15)
        resp.raise_for_status()
        return resp.json().get("market", {})
    except Exception as e:
        log.error(f"get_market({ticker}) failed: {e}")
        return {}


def get_events(limit=100, status="open", with_markets=True):
    """Fetch events (groups of related markets)."""
    params = {
        "limit": limit,
        "status": status,
        "with_nested_markets": str(with_markets).lower(),
    }
    try:
        resp = _session.get(f"{KALSHI_HOST}/events", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        log.error(f"get_events failed: {e}")
        return []


def get_orderbook(ticker: str) -> dict:
    """Fetch order book for a market."""
    try:
        resp = _session.get(f"{KALSHI_HOST}/markets/{ticker}/orderbook", timeout=10)
        resp.raise_for_status()
        return resp.json().get("orderbook", {})
    except Exception as e:
        log.error(f"get_orderbook({ticker}) failed: {e}")
        return {}


# ── Authenticated endpoints ──────────────────────────────────────────────────

def get_balance() -> float:
    """Return balance in USD (converts from cents)."""
    if not _portfolio_api:
        return 0.0
    try:
        resp = _portfolio_api.get_balance()
        return resp.balance / 100
    except Exception as e:
        log.error(f"get_balance failed: {e}")
        return None   # None = unknown (different from 0.0 = confirmed broke)


def get_positions() -> list:
    """Return open positions."""
    if not _portfolio_api:
        return []
    try:
        resp = _portfolio_api.get_positions()
        return resp.market_positions or []
    except Exception as e:
        log.error(f"get_positions failed: {e}")
        return []


def place_order(ticker: str, side: str, count: int, price_cents: int,
                order_type: str = "limit") -> dict:
    """
    Place an order.
    side: 'yes' or 'no'
    count: number of contracts
    price_cents: price in cents (1-99)
    """
    if not _portfolio_api:
        log.error("Cannot place order — not authenticated.")
        return {}
    try:
        from kalshi_python import CreateOrderRequest
        req = CreateOrderRequest(
            ticker=ticker,
            side=side,
            count=count,
            type=order_type,
            yes_price=price_cents if side == "yes" else None,
            no_price=price_cents if side == "no" else None,
        )
        resp = _portfolio_api.create_order(req)
        log.info(f"Order placed: {ticker} {side} {count}x @ {price_cents}¢")
        return resp.to_dict() if hasattr(resp, "to_dict") else {}
    except Exception as e:
        log.error(f"place_order failed: {e}")
        return {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def cents_to_usd(cents: int) -> float:
    return cents / 100


def usd_to_contracts(usd: float, price_cents: int) -> int:
    """How many contracts can we buy for $usd at price_cents?"""
    if price_cents <= 0:
        return 0
    return max(1, int((usd * 100) // price_cents))


def market_url(ticker: str) -> str:
    return f"https://kalshi.com/markets/{ticker}"
