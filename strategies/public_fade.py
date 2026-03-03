"""
Public Fade Strategy
====================
Fade the public on Kalshi sports game-winner markets, confirmed by Polymarket whale activity.

Logic:
  1. Find Kalshi sports game-winner markets where YES is in the "public overbetting" zone
     (default 63–80¢ — favorite heavily backed but not a lock)
  2. Find matching Polymarket market via team-name entity matching
  3. Require whale confirmation: smart money must have recently bought the UNDERDOG side
     on Polymarket (price < 0.50 = backing the dog)
  4. If confirmed → buy NO on Kalshi (fade the public favorite)

Why it works:
  - Public bettors systematically overvalue favorites, popular teams, home teams
  - Kalshi price reflects this bias — YES gets pushed above fair value
  - Polymarket whales (smart money) tend to be on the correct side
  - Convergence of public fade + whale confirmation = high-conviction contrarian entry
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import (
    SUPABASE_URL, ANON_KEY, HEADERS,
    get_top_traders, get_whale_trades, get_trader_stats,
    is_sports_title, extract_team_tokens, match_kalshi_market,
    load_seen, save_seen,
)
from core import api, notifier, state as state_mgr

SEEN_FILE = os.path.join(os.path.dirname(__file__), "..", "public_fade_seen.json")

_session = requests.Session()
_session.headers.update(HEADERS)


def get_recent_whale_trades_for_game(game_keywords: list, lookback_hours: int = 6,
                                      min_usd: float = 200) -> list:
    """
    Fetch recent whale trades from PolymarketScan and filter to those
    matching the target game (via keyword overlap on market_title).
    Returns trades where the whale was on the UNDERDOG side (price < 0.50).
    """
    from core.health import cached

    def fetch():
        r = _session.get(
            f"{SUPABASE_URL}/whale_trades_cache",
            params={
                "select": "tx_hash,wallet,market_title,market_slug,side,outcome,"
                          "amount_usd,price,timestamp,tier,anomaly_tags",
                "amount_usd": f"gte.{min_usd}",
                "order": "timestamp.desc",
                "limit": 100,
            }, timeout=10,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []

    all_trades = cached("whale_trades_fade", ttl=120, fetch_fn=fetch) or []

    now_ts = time.time()
    cutoff = now_ts - (lookback_hours * 3600)

    matching = []
    for t in all_trades:
        # Time filter
        ts_str = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                continue
        except Exception:
            continue

        # Only BUY trades on the underdog side (price < 0.50)
        side = t.get("side", "").upper()
        price = t.get("price", 1.0)
        if side != "BUY":
            continue
        if price >= 0.50:
            continue  # whale backed the favorite — not what we want

        # Keyword match to the target game
        pm_title = (t.get("market_title") or "").lower()
        hits = sum(1 for kw in game_keywords if kw in pm_title)
        if hits >= 2:
            matching.append(t)

    return matching


class PublicFadeStrategy(BaseStrategy):
    name = "public_fade"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        min_yes          = cfg.get("min_yes_price", 63)   # public-favorite floor (cents)
        max_yes          = cfg.get("max_yes_price", 80)   # above this = genuine lock, not public bias
        max_pos          = cfg.get("max_position_usd", 20)
        min_volume       = cfg.get("min_volume_24h", 1000)
        max_hours        = cfg.get("max_hours_to_close", 48)
        lookback_hours   = cfg.get("whale_lookback_hours", 6)
        min_whale_usd    = cfg.get("min_whale_trade_usd", 200)
        min_whale_trades = cfg.get("min_whale_confirmations", 1)
        risk             = state.get("_risk_config", {})

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=max_hours)

        # ── Step 1: Find Kalshi sports markets in the public-overbetting zone ──
        candidates = []
        for m in markets:
            cat      = m.get("_category", "").lower()
            title    = m.get("title", "")
            ticker   = m.get("ticker", "")
            yes_ask  = m.get("yes_ask", 0)
            no_ask   = m.get("no_ask", 0)
            vol      = m.get("volume_24h", 0)
            close_time = m.get("close_time") or m.get("expiration_time", "")

            # Sports only
            if cat != "sports":
                continue

            # Must look like a game-winner market
            title_lower = title.lower()
            if not any(kw in title_lower for kw in ("winner", "win", "vs")):
                continue

            # Public-overbetting zone: YES is pricey but not a lock
            if not (min_yes <= yes_ask <= max_yes):
                continue

            # Need liquidity
            if vol < min_volume:
                continue

            # Must close soon
            if not close_time:
                continue
            try:
                closes_at = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except Exception:
                continue
            if closes_at <= now or closes_at > cutoff:
                continue

            if self.is_already_open(state, ticker):
                continue

            hours_left = (closes_at - now).total_seconds() / 3600

            candidates.append({
                "ticker":     ticker,
                "title":      title,
                "yes_ask":    yes_ask,
                "no_ask":     no_ask,
                "vol":        vol,
                "hours_left": round(hours_left, 1),
                "closes_at":  closes_at,
            })

        if not candidates:
            self.log.info("No public-overbetting candidates found this tick.")
            return

        self.log.info(f"{len(candidates)} public-favorite candidates to check for whale fade confirmation.")

        # ── Step 2: For each candidate, check Polymarket whale confirmation ──
        confirmed = []
        for cand in candidates:
            title = cand["title"]

            # Extract team keywords for PM search
            team_sets = extract_team_tokens(title)
            # Build flat keyword list from all team aliases
            game_keywords = [alias for ts in team_sets for alias in ts]
            # Also add raw words from title (stripped of stopwords)
            stopwords = {"will", "the", "a", "winner", "win", "vs", "?", "who"}
            raw_words = [w.lower() for w in title.split() if w.lower() not in stopwords and len(w) > 2]
            game_keywords = list(set(game_keywords + raw_words))

            if not game_keywords:
                self.log.info(f"No game keywords extracted for: {title[:60]}")
                continue

            whale_confirms = get_recent_whale_trades_for_game(
                game_keywords,
                lookback_hours=lookback_hours,
                min_usd=min_whale_usd,
            )

            if len(whale_confirms) < min_whale_trades:
                self.log.info(
                    f"👀 Candidate (no whale yet): {title[:60]} "
                    f"YES@{cand['yes_ask']}¢ | closes in {cand['hours_left']}h"
                )
                continue

            # Build confirmation summary
            total_whale_usd = sum(t.get("amount_usd", 0) for t in whale_confirms)
            top_whale = whale_confirms[0]
            stats = get_trader_stats(top_whale.get("wallet", ""))
            trader_name = stats.get("display_name") or top_whale.get("wallet", "")[:10] + "..."
            trader_pnl  = stats.get("total_pnl", 0)
            trader_wr   = stats.get("win_rate", 0)

            cand["whale_confirms"]   = len(whale_confirms)
            cand["whale_usd"]        = total_whale_usd
            cand["trader_name"]      = trader_name
            cand["trader_pnl"]       = trader_pnl
            cand["trader_wr"]        = trader_wr
            cand["pm_price"]         = top_whale.get("price", 0)
            confirmed.append(cand)

            self.log.info(
                f"✅ Fade confirmed: {title[:60]} | "
                f"YES@{cand['yes_ask']}¢ (public) | "
                f"{len(whale_confirms)} whale(s) on underdog (${total_whale_usd:,.0f})"
            )

        if not confirmed:
            self.log.info("No confirmed fade opportunities this tick (candidates found but no whale signal).")
            return

        # Sort: most whale conviction first
        confirmed.sort(key=lambda x: x["whale_usd"], reverse=True)

        # ── Step 3: Execute — buy NO (fading the public favorite) ────────────
        for opp in confirmed[:3]:
            if not self.can_open(state, risk, max_pos, cfg):
                break

            ticker    = opp["ticker"]
            title     = opp["title"]
            no_ask    = opp["no_ask"]
            yes_ask   = opp["yes_ask"]

            if no_ask <= 0:
                continue

            contracts = api.usd_to_contracts(max_pos, no_ask)
            url       = self.market_url(ticker)

            # Expected return if NO wins: (100 - no_ask) / no_ask
            expected_ret = round((100 - no_ask) / no_ask * 100, 1)

            detail = (
                f"📊 Public on YES @ {yes_ask}¢ — fading with NO @ {no_ask}¢\n"
                f"Expected return: +{expected_ret:.1f}% | Closes in: {opp['hours_left']}h\n"
                f"🐋 {opp['whale_confirms']} whale confirm(s) — ${opp['whale_usd']:,.0f} on underdog\n"
                f"Top: {opp['trader_name']} | PnL ${opp['trader_pnl']:,.0f} | WR {opp['trader_wr']:.0f}%\n"
                f"PM underdog price: {opp['pm_price']:.0%}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] FADE: {title[:55]} | "
                f"NO@{no_ask}¢ | +{expected_ret:.1f}% | {opp['whale_confirms']} whale(s)"
            )
            notifier.opportunity_alert("📉 Public Fade", title[:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name,
                                        "NO", no_ask, max_pos)
            else:
                order = api.place_order(ticker, "no", contracts, no_ask)
                if order is not None:
                    state_mgr.open_position(state, ticker, self.name,
                                            "NO", no_ask, max_pos)
