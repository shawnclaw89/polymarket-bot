"""
Polymarket Tail Strategy
========================
Uses Polymarket as a FREE signal source (read-only, no trading there),
then executes equivalent trades on Kalshi.

Since Polymarket is on-chain, all wallet activity is public.
We track known profitable whale wallets via Polymarket's data API,
detect when they open positions, then find the matching Kalshi market
and tail the trade.

Flow:
  1. Poll wallet activity for each watched address
  2. Detect new BUY trades above min_trade_usd
  3. Match the Polymarket market title → Kalshi market (fuzzy match)
  4. Execute the same directional bet on Kalshi

Config:
  watch_wallets: list of Polygon wallet addresses to monitor
  Use python3 bot.py --discover-whales to auto-find large traders
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
SEEN_FILE = os.path.join(os.path.dirname(__file__), "..", "polymarket_seen.json")

_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-bot/1.0"})


# ── Polymarket data helpers ──────────────────────────────────────────────────

def get_wallet_activity(address: str, limit: int = 20) -> list:
    """Fetch recent activity for a Polymarket wallet."""
    try:
        r = _session.get(
            f"{DATA_API}/activity",
            params={"user": address.lower(), "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception:
        return []


def get_wallet_positions(address: str) -> list:
    """Fetch current open positions for a wallet."""
    try:
        r = _session.get(
            f"{DATA_API}/positions",
            params={"user": address.lower(), "sizeThreshold": 10},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []


def discover_whales(min_trade_usd: float = 5000, limit: int = 200) -> list:
    """
    Auto-discover whale wallets by scanning Polymarket markets for large recent
    volume and collecting the wallet addresses making big moves.
    Returns a list of addresses sorted by trade size.
    """
    try:
        r = _session.get(
            f"{GAMMA_API}/markets",
            params={"limit": limit, "order": "volume24hr", "active": "true", "closed": "false"},
            timeout=15,
        )
        r.raise_for_status()
        markets = r.json()
    except Exception:
        return []

    # Collect any wallet addresses from submitted_by fields with high volume
    whale_candidates = {}
    for m in markets:
        wallet = m.get("submitted_by", "")
        vol = m.get("volumeNum", 0)
        if wallet and vol >= min_trade_usd:
            whale_candidates[wallet.lower()] = max(
                whale_candidates.get(wallet.lower(), 0), vol
            )

    return sorted(whale_candidates.keys(), key=lambda w: whale_candidates[w], reverse=True)[:20]


# ── Market matching ──────────────────────────────────────────────────────────

def match_kalshi_market(polymarket_title: str, kalshi_markets: list) -> dict | None:
    """
    Fuzzy-match a Polymarket market title to a Kalshi market.
    Returns the best match or None.
    """
    if not polymarket_title or not kalshi_markets:
        return None

    title_lower = polymarket_title.lower()

    # Extract keywords (skip common words)
    stopwords = {"will", "the", "a", "an", "be", "is", "in", "on", "by", "of",
                 "to", "or", "and", "for", "at", "it", "vs", "win", "wins"}
    keywords = [w for w in title_lower.split() if len(w) > 2 and w not in stopwords]

    best_match = None
    best_score = 0

    for km in kalshi_markets:
        km_title = (km.get("title") or "").lower()
        if not km_title:
            continue

        # Count keyword hits
        hits = sum(1 for kw in keywords if kw in km_title)
        score = hits / max(len(keywords), 1)

        if score > best_score and score >= 0.4:  # 40% keyword overlap minimum
            best_score = score
            best_match = km

    return best_match


# ── Seen trade tracking ──────────────────────────────────────────────────────

def load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


# ── Strategy ─────────────────────────────────────────────────────────────────

class PolymarketTailStrategy(BaseStrategy):
    name = "polymarket_tail"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        watch_wallets   = cfg.get("watch_wallets", [])
        min_trade_usd   = cfg.get("min_trade_usd", 500)
        max_pos         = cfg.get("max_position_usd", 30)
        copy_ratio      = cfg.get("copy_ratio", 0.1)
        auto_discover   = cfg.get("auto_discover_whales", True)
        risk            = state.get("_risk_config", {})

        # Auto-discover whales if no wallets configured
        if not watch_wallets and auto_discover:
            self.log.info("No wallets configured — auto-discovering whales...")
            watch_wallets = discover_whales(min_trade_usd=min_trade_usd)
            if watch_wallets:
                self.log.info(f"Discovered {len(watch_wallets)} whale candidates: "
                              f"{[w[:10]+'...' for w in watch_wallets[:5]]}")

        if not watch_wallets:
            self.log.info("No whale wallets to watch. Add addresses to config.yaml.")
            return

        seen = load_seen()
        new_signals = []

        for wallet in watch_wallets:
            activity = get_wallet_activity(wallet, limit=10)

            for trade in activity:
                tx_hash  = trade.get("transactionHash", "")
                trade_type = trade.get("type", "")
                side     = trade.get("side", "").upper()
                usd_size = trade.get("usdcSize", 0) or 0
                price    = trade.get("price", 0) or 0
                title    = trade.get("title", "")
                slug     = trade.get("slug", "")
                ts       = trade.get("timestamp", 0)

                # Only look at BUY trades above threshold
                if trade_type != "TRADE" or side != "BUY":
                    continue
                if usd_size < min_trade_usd:
                    continue

                # Skip if we've already seen this transaction
                if tx_hash and tx_hash in seen:
                    continue

                # Skip if trade is older than 2 hours
                if ts and (time.time() - ts) > 7200:
                    continue

                new_signals.append({
                    "wallet": wallet,
                    "tx_hash": tx_hash,
                    "side": side,
                    "usd_size": usd_size,
                    "price": price,
                    "title": title,
                    "slug": slug,
                    "ts": ts,
                })

                if tx_hash:
                    seen[tx_hash] = int(time.time())

        # Prune old seen entries (older than 48h)
        cutoff = int(time.time()) - 172800
        seen = {k: v for k, v in seen.items() if v > cutoff}
        save_seen(seen)

        if not new_signals:
            return

        self.log.info(f"Found {len(new_signals)} new whale signals.")

        for sig in new_signals[:5]:
            # Match to Kalshi market
            kalshi_match = match_kalshi_market(sig["title"], markets)

            if not kalshi_match:
                self.log.info(f"No Kalshi match for: {sig['title'][:60]}")
                notifier.send(
                    f"🐋 Polymarket Whale Alert (no Kalshi match)\n"
                    f"Wallet: {sig['wallet'][:10]}...\n"
                    f"BUY ${sig['usd_size']:,.0f} @ {sig['price']:.0%}\n"
                    f"Market: {sig['title'][:80]}\n"
                    f"🔗 https://polymarket.com/event/{sig['slug']}"
                )
                continue

            ticker     = kalshi_match.get("ticker", "")
            k_title    = kalshi_match.get("title", "")
            yes_ask    = kalshi_match.get("yes_ask", 0)
            trade_size = min(sig["usd_size"] * copy_ratio, max_pos)

            if self.is_already_open(state, ticker):
                continue
            if not self.can_open(state, risk, trade_size):
                break

            contracts = api.usd_to_contracts(trade_size, yes_ask) if yes_ask > 0 else 0
            url = api.market_url(ticker)

            detail = (
                f"Mirroring whale {sig['wallet'][:10]}...\n"
                f"Polymarket: {sig['title'][:60]}\n"
                f"BUY ${sig['usd_size']:,.0f} → Kalshi: {k_title[:60]}\n"
                f"YES @ {yes_ask}¢ | Mirror size: ${trade_size:.0f}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] Tailing whale: "
                f"{sig['title'][:50]} → {ticker} | ${trade_size:.0f}"
            )
            notifier.opportunity_alert("🐋 Polymarket Tail", k_title[:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name,
                                        "YES", yes_ask, trade_size)
            else:
                if contracts > 0:
                    api.place_order(ticker, "yes", contracts, yes_ask)
                state_mgr.open_position(state, ticker, self.name,
                                        "YES", yes_ask, trade_size)
