"""
Polymarket Tail Strategy (powered by PolymarketScan)
=====================================================
Uses PolymarketScan's public Supabase database to:

  1. Fetch top-ranked traders by total PnL / alpha score / win rate
  2. Watch whale_trades_cache for their latest BUY trades (live, <5 min delay)
  3. Match Polymarket market titles → Kalshi equivalents (fuzzy keyword match)
  4. Mirror the trade on Kalshi at a configurable copy ratio

Data sources (all public, no auth required):
  - trader_metrics_full  → rank traders by alpha, PnL, win rate
  - whale_trades_cache   → live whale trades with wallet + market info
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr

SUPABASE_URL = "https://gzydspfquuaudqeztorw.supabase.co/rest/v1"
ANON_KEY     = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd6eWRzcGZxdXVhdWRxZXp0b3J3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjQ4OTI5NjUsImV4cCI6MjA4MDQ2ODk2NX0"
    ".97m7q4bYcy8xU-OqcuAeHytV45XFm8ddLhSu39Ztvmk"
)
HEADERS = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}

SEEN_FILE = os.path.join(os.path.dirname(__file__), "..", "polymarket_seen.json")

_session = requests.Session()
_session.headers.update(HEADERS)


# ── PolymarketScan API helpers ───────────────────────────────────────────────

def get_top_traders(limit: int = 50, min_pnl: float = 1000,
                    min_win_rate: float = 55, min_trades: int = 20) -> list:
    """
    Fetch top-performing traders from PolymarketScan ranked by alpha_score.
    Returns list of wallet addresses.
    """
    try:
        r = _session.get(
            f"{SUPABASE_URL}/trader_metrics_full",
            params={
                "select": "wallet,display_name,total_pnl,roi_percent,win_rate,"
                          "alpha_score,conviction_score,trade_count,market_focus",
                "total_pnl": f"gte.{min_pnl}",
                "win_rate":  f"gte.{min_win_rate}",
                "trade_count": f"gte.{min_trades}",
                "order": "alpha_score.desc",
                "limit": limit,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        return []


def get_whale_trades(min_usd: float = 500, limit: int = 50,
                     watch_wallets: list = None) -> list:
    """
    Fetch recent whale trades from PolymarketScan.
    Optionally filter to specific wallet addresses.
    """
    try:
        params = {
            "select": "tx_hash,wallet,market_title,market_slug,side,outcome,"
                      "amount_usd,price,timestamp,tier,anomaly_tags",
            "amount_usd": f"gte.{min_usd}",
            "order": "timestamp.desc",
            "limit": limit,
        }
        # Filter to specific wallets if provided
        if watch_wallets:
            params["wallet"] = f"in.({','.join(watch_wallets)})"

        r = _session.get(f"{SUPABASE_URL}/whale_trades_cache", params=params, timeout=10)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        return []


def get_trader_stats(wallet: str) -> dict:
    """Get detailed stats for a specific wallet."""
    try:
        r = _session.get(
            f"{SUPABASE_URL}/trader_metrics_full",
            params={"wallet": f"eq.{wallet.lower()}", "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else {}
    except Exception:
        return {}


# ── Market matching ──────────────────────────────────────────────────────────

def match_kalshi_market(pm_title: str, pm_slug: str, kalshi_markets: list) -> dict | None:
    """Fuzzy match Polymarket title/slug → best Kalshi market."""
    if not pm_title or not kalshi_markets:
        return None

    title_lower = pm_title.lower()
    slug_lower  = (pm_slug or "").lower().replace("-", " ")

    stopwords = {"will", "the", "a", "an", "be", "is", "in", "on", "by", "of",
                 "to", "or", "and", "for", "at", "it", "win", "wins", "2026",
                 "vs", "does", "did", "has", "have", "reach", "hit"}

    keywords = [w for w in (title_lower + " " + slug_lower).split()
                if len(w) > 2 and w not in stopwords]

    best, best_score = None, 0.0

    for km in kalshi_markets:
        km_title = (km.get("title") or "").lower()
        if not km_title:
            continue
        hits = sum(1 for kw in keywords if kw in km_title)
        score = hits / max(len(keywords), 1)
        if score > best_score and score >= 0.35:
            best_score = score
            best = km

    return best


# ── Seen tracking ────────────────────────────────────────────────────────────

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
        min_trade_usd    = cfg.get("min_trade_usd", 500)
        max_pos          = cfg.get("max_position_usd", 30)
        copy_ratio       = cfg.get("copy_ratio", 0.10)
        min_trader_pnl   = cfg.get("min_trader_pnl", 1000)
        min_win_rate     = cfg.get("min_trader_win_rate", 55)
        min_trades       = cfg.get("min_trader_trades", 20)
        watch_wallets    = cfg.get("watch_wallets", [])
        only_buy         = cfg.get("only_buy_side", True)
        risk             = state.get("_risk_config", {})

        # ── Step 1: Build/use whale wallet list ──────────────────────────────
        if not watch_wallets:
            self.log.info("Fetching top traders from PolymarketScan...")
            traders = get_top_traders(
                limit=30,
                min_pnl=min_trader_pnl,
                min_win_rate=min_win_rate,
                min_trades=min_trades,
            )
            watch_wallets = [t["wallet"] for t in traders if t.get("wallet")]
            if traders:
                self.log.info(
                    f"Tracking {len(watch_wallets)} top traders | "
                    f"Top: {traders[0].get('display_name') or traders[0]['wallet'][:10]}... "
                    f"PnL=${traders[0].get('total_pnl',0):,.0f} "
                    f"WR={traders[0].get('win_rate',0):.0f}%"
                )

        if not watch_wallets:
            self.log.info("No whale wallets available — skipping.")
            return

        # ── Step 2: Fetch latest whale trades ────────────────────────────────
        whale_trades = get_whale_trades(
            min_usd=min_trade_usd,
            limit=50,
            watch_wallets=watch_wallets if len(watch_wallets) <= 20 else None,
        )

        if not whale_trades:
            self.log.debug("No whale trades found this tick.")
            return

        self.log.info(f"Got {len(whale_trades)} whale trades to evaluate.")

        # ── Step 3: Filter unseen BUY trades ─────────────────────────────────
        seen     = load_seen()
        now_ts   = time.time()
        cutoff   = now_ts - (cfg.get("lookback_hours", 2) * 3600)

        new_trades = []
        for t in whale_trades:
            tx    = t.get("tx_hash", "")
            side  = t.get("side", "").upper()
            ts_str = t.get("timestamp", "")

            if tx in seen:
                continue
            if only_buy and side != "BUY":
                continue

            # Parse timestamp
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if ts < cutoff:
                    continue
            except Exception:
                continue

            # Skip if wallet not in our watch list (when watch_wallets was set)
            if cfg.get("watch_wallets") and t.get("wallet") not in watch_wallets:
                continue

            new_trades.append(t)
            if tx:
                seen[tx] = int(now_ts)

        # Prune seen older than 48h
        seen = {k: v for k, v in seen.items() if v > now_ts - 172800}
        save_seen(seen)

        if not new_trades:
            self.log.debug("No new whale trades since last scan.")
            return

        self.log.info(f"{len(new_trades)} new whale trades found.")

        # ── Step 4: Match to Kalshi + execute ────────────────────────────────
        for trade in new_trades[:5]:
            wallet     = trade.get("wallet", "")
            pm_title   = trade.get("market_title", "")
            pm_slug    = trade.get("market_slug", "")
            side       = trade.get("side", "BUY").upper()
            outcome    = trade.get("outcome", "Yes")
            amount_usd = trade.get("amount_usd", 0)
            price      = trade.get("price", 0.5)
            tier       = trade.get("tier", "unknown")
            anomaly    = trade.get("anomaly_tags", [])

            kalshi_match = match_kalshi_market(pm_title, pm_slug, markets)

            if not kalshi_match:
                self.log.info(f"No Kalshi match: {pm_title[:60]}")
                # Still alert — useful intel even without Kalshi trade
                notifier.send(
                    f"🐋 Polymarket Whale [{tier.upper()}]\n"
                    f"{side} ${amount_usd:,.0f} @ {price:.0%}\n"
                    f"Outcome: {outcome}\n"
                    f"Market: {pm_title[:80]}\n"
                    f"{'⚡ ANOMALY: ' + ', '.join(anomaly) if anomaly else ''}\n"
                    f"🔗 https://polymarket.com/event/{pm_slug}\n"
                    f"_(No Kalshi match — info only)_"
                )
                continue

            ticker   = kalshi_match.get("ticker", "")
            k_title  = kalshi_match.get("title", "")
            yes_ask  = kalshi_match.get("yes_ask", 0)
            no_ask   = kalshi_match.get("no_ask", 0)

            # Determine which side to take on Kalshi
            kalshi_side = "YES" if outcome.lower() in ("yes", "y") else "NO"
            entry_cents = yes_ask if kalshi_side == "YES" else no_ask

            if entry_cents <= 0:
                continue
            if self.is_already_open(state, ticker):
                continue

            trade_size = min(amount_usd * copy_ratio, max_pos)
            if not self.can_open(state, risk, trade_size):
                break

            contracts = api.usd_to_contracts(trade_size, entry_cents)
            url       = api.market_url(ticker)

            # Get trader stats for context
            stats = get_trader_stats(wallet)
            trader_name = (stats.get("display_name") or wallet[:10] + "...")
            trader_pnl  = stats.get("total_pnl", 0)
            trader_wr   = stats.get("win_rate", 0)

            detail = (
                f"🐋 [{tier.upper()}] {trader_name}\n"
                f"PnL: ${trader_pnl:,.0f} | Win rate: {trader_wr:.0f}%\n"
                f"PM trade: {side} ${amount_usd:,.0f} @ {price:.0%} → {outcome}\n"
                f"Kalshi: {kalshi_side} @ {entry_cents}¢ | Size: ${trade_size:.0f}\n"
                + (f"⚡ {', '.join(anomaly)}" if anomaly else "")
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] Tailing {trader_name}: "
                f"{pm_title[:50]} → {ticker} | {kalshi_side}@{entry_cents}¢"
            )
            notifier.opportunity_alert("🐋 Polymarket Tail", k_title[:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name,
                                        kalshi_side, entry_cents, trade_size)
            else:
                api.place_order(ticker, kalshi_side.lower(), contracts, entry_cents)
                state_mgr.open_position(state, ticker, self.name,
                                        kalshi_side, entry_cents, trade_size)
