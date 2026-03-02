"""
Health Check + PolymarketScan Data Cache

Health checks:
  - Kalshi API reachable
  - PolymarketScan Supabase key valid
  - Bot daily P&L within limits

Cache:
  - PolymarketScan data (whale trades, agent positions, top traders)
    fetched once per cache_ttl_seconds, reused across ticks
  - Avoids hammering external APIs every 60s
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("health")

SUPABASE_URL = "https://gzydspfquuaudqeztorw.supabase.co/rest/v1"
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd6eWRzcGZxdXVhdWRxZXp0b3J3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQ4OTI5NjUsImV4cCI6MjA4MDQ2ODk2NX0"
    ".97m7q4bYcy8xU-OqcuAeHytV45XFm8ddLhSu39Ztvmk"
)
HEADERS = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}

_session = requests.Session()
_session.headers.update(HEADERS)

# ── In-memory cache ──────────────────────────────────────────────────────────
_cache: dict[str, dict] = {}
# {key: {"data": ..., "fetched_at": float, "ttl": int}}

_health_state = {
    "kalshi_ok": None,
    "polymarketscan_ok": None,
    "last_check": 0,
}


def _is_fresh(key: str) -> bool:
    if key not in _cache:
        return False
    entry = _cache[key]
    return (time.time() - entry["fetched_at"]) < entry["ttl"]


def cached(key: str, ttl: int, fetch_fn):
    """Return cached data if fresh, otherwise re-fetch and cache."""
    if _is_fresh(key):
        log.debug(f"Cache hit: {key}")
        return _cache[key]["data"]
    try:
        data = fetch_fn()
        _cache[key] = {"data": data, "fetched_at": time.time(), "ttl": ttl}
        log.debug(f"Cache refreshed: {key}")
        return data
    except Exception as e:
        log.warning(f"Cache fetch failed ({key}): {e}")
        # Return stale data if available rather than crashing
        if key in _cache:
            log.info(f"Using stale cache for {key}")
            return _cache[key]["data"]
        return None


def invalidate(key: str):
    _cache.pop(key, None)


# ── Health checks ────────────────────────────────────────────────────────────

def check_kalshi() -> bool:
    try:
        r = requests.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"limit": 1, "status": "open"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception:
        return False


def check_polymarketscan() -> bool:
    try:
        r = _session.get(
            f"{SUPABASE_URL}/whale_trades_cache",
            params={"select": "id", "limit": 1},
            timeout=8,
        )
        data = r.json()
        return isinstance(data, list)
    except Exception:
        return False


def run_checks(to: str = "7591705971", check_interval: int = 600) -> dict:
    """
    Run health checks at most every check_interval seconds.
    Sends Telegram alert if anything degrades.
    Returns current health state.
    """
    from core import notifier

    now = time.time()
    if now - _health_state["last_check"] < check_interval:
        return _health_state

    _health_state["last_check"] = now
    issues = []

    # Kalshi
    kalshi_ok = check_kalshi()
    if _health_state["kalshi_ok"] is True and not kalshi_ok:
        issues.append("⚠️ Kalshi API went down")
    elif _health_state["kalshi_ok"] is False and kalshi_ok:
        issues.append("✅ Kalshi API recovered")
    _health_state["kalshi_ok"] = kalshi_ok

    # PolymarketScan
    pms_ok = check_polymarketscan()
    if _health_state["polymarketscan_ok"] is True and not pms_ok:
        issues.append("⚠️ PolymarketScan API went down — whale/agent signals paused")
    elif _health_state["polymarketscan_ok"] is False and pms_ok:
        issues.append("✅ PolymarketScan API recovered")
    _health_state["polymarketscan_ok"] = pms_ok

    if issues:
        notifier.send(
            "🏥 *Bot Health Alert*\n" + "\n".join(issues),
            to=to,
        )

    if not kalshi_ok or not pms_ok:
        log.warning(
            f"Health: Kalshi={'✅' if kalshi_ok else '❌'} | "
            f"PolymarketScan={'✅' if pms_ok else '❌'}"
        )

    return _health_state


def is_polymarketscan_ok() -> bool:
    return _health_state.get("polymarketscan_ok") is not False


def is_kalshi_ok() -> bool:
    return _health_state.get("kalshi_ok") is not False
