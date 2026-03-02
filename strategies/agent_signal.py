"""
Agent vs Market Signal Strategy
=================================
Finds markets where AI agents on Polymarket are MORE BULLISH than the current
market price — meaning agents collectively think a market is underpriced
relative to what the crowd is pricing in.

Signal logic:
  1. Group all agent arena positions by market
  2. Calculate agents' collective consensus side + average entry price
  3. Compare agents' avg entry price vs current Polymarket market price
  4. When agents are materially more bullish than the market → potential edge
  5. Stronger signal when BOTH agents AND human whales are on the same side

Why this works:
  Even though most individual agents are losing, their collective entry price
  represents a distributed AI estimate of fair value. When multiple agents
  independently chose to buy at prices the market has since moved below,
  that's a signal the market may have overreacted.

  When human whale confirmation aligns → strongest signal.
"""
import json
import os
import time
from collections import defaultdict
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
HEADERS  = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}
_session = requests.Session()
_session.headers.update(HEADERS)

# Agents that have shown they can at least preserve capital (not bankrupt)
GOOD_AGENTS = {
    "black-widow", "PaperTradingCo", "BettyBot", "AmirBot",
    "OpenClawBot", "polybot-paper", "EchoBot",
}


def get_agent_positions() -> list:
    try:
        r = _session.get(
            f"{SUPABASE_URL}/agent_arena_positions",
            params={"select": "agent_id,market_slug,market_title,side,shares,avg_price",
                    "limit": 500},
            timeout=10,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception:
        return []


def get_agent_balances() -> dict:
    """Return {agent_id: roi_pct} for all agents."""
    try:
        r = _session.get(
            f"{SUPABASE_URL}/agent_arena_balances",
            params={"select": "agent_id,balance,initial_balance", "limit": 200},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json() if isinstance(r.json(), list) else []
        return {
            a["agent_id"]: ((a["balance"] - a["initial_balance"]) / a["initial_balance"] * 100)
            for a in data if a.get("initial_balance", 0) > 0
        }
    except Exception:
        return {}


def get_recent_whale_buys(min_usd: float = 300, hours: int = 6) -> dict:
    """Return {market_slug: total_whale_usd} for recent BUY trades."""
    cutoff = datetime.now(timezone.utc).isoformat()
    try:
        r = _session.get(
            f"{SUPABASE_URL}/whale_trades_cache",
            params={
                "select": "market_slug,amount_usd,side",
                "side":   "eq.BUY",
                "amount_usd": f"gte.{min_usd}",
                "order": "timestamp.desc",
                "limit": 200,
            },
            timeout=10,
        )
        r.raise_for_status()
        trades = r.json() if isinstance(r.json(), list) else []
        result = defaultdict(float)
        for t in trades:
            result[t.get("market_slug", "")] += t.get("amount_usd", 0)
        return dict(result)
    except Exception:
        return {}


def get_polymarket_price(slug: str) -> float | None:
    """Fetch current YES price from Polymarket Gamma API."""
    try:
        r = _session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        markets = r.json()
        if not markets:
            return None
        prices_raw = markets[0].get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        return float(prices[0]) if prices else None
    except Exception:
        return None


def match_kalshi_market(pm_title: str, pm_slug: str, kalshi_markets: list) -> dict | None:
    if not pm_title or not kalshi_markets:
        return None
    stopwords = {"will", "the", "a", "an", "be", "is", "in", "on", "by", "of",
                 "to", "or", "and", "for", "at", "it", "win", "wins", "2026",
                 "vs", "does", "before"}
    combined = (pm_title + " " + pm_slug.replace("-", " ")).lower()
    keywords = [w for w in combined.split() if len(w) > 2 and w not in stopwords]
    best, best_score = None, 0.0
    for km in kalshi_markets:
        km_title = (km.get("title") or "").lower()
        if not km_title:
            continue
        hits = sum(1 for kw in keywords if kw in km_title)
        score = hits / max(len(keywords), 1)
        if score > best_score and score >= 0.35:
            best_score, best = score, km
    return best


class AgentSignalStrategy(BaseStrategy):
    name = "agent_signal"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        min_agents          = cfg.get("min_agents_agree", 2)
        min_bullish_gap     = cfg.get("min_bullish_gap", 0.08)  # agents avg > mkt by this much
        require_whale_conf  = cfg.get("require_whale_confirmation", False)
        min_whale_usd       = cfg.get("min_whale_confirmation_usd", 300)
        max_pos             = cfg.get("max_position_usd", 25)
        weight_good_agents  = cfg.get("weight_profitable_agents", True)
        risk                = state.get("_risk_config", {})

        # ── Fetch data ───────────────────────────────────────────────────────
        positions   = get_agent_positions()
        roi_by_agent = get_agent_balances()
        whale_buys  = get_recent_whale_buys(min_usd=min_whale_usd) if require_whale_conf else {}

        if not positions:
            self.log.debug("No agent positions found.")
            return

        # ── Aggregate agent consensus per market ─────────────────────────────
        # market_slug → {side: [{agent, price, weight}]}
        by_market = defaultdict(lambda: {"yes": [], "no": [], "title": "", "slug": ""})

        for pos in positions:
            slug  = pos.get("market_slug", "")
            title = pos.get("market_title", "")
            side  = pos.get("side", "").lower()
            price = pos.get("avg_price", 0) or 0
            agent = pos.get("agent_id", "")

            if not slug or side not in ("yes", "no") or price <= 0:
                continue

            roi = roi_by_agent.get(agent, -50)
            # Weight: good agents count more, bankrupt agents count less
            if weight_good_agents:
                if agent in GOOD_AGENTS:
                    weight = 2.0
                elif roi > -10:
                    weight = 1.5
                elif roi > -30:
                    weight = 1.0
                elif roi > -60:
                    weight = 0.5
                else:
                    weight = 0.2   # still counts but heavily discounted
            else:
                weight = 1.0

            by_market[slug]["title"] = title
            by_market[slug]["slug"]  = slug
            by_market[slug][side].append({
                "agent": agent, "price": price, "weight": weight, "roi": roi
            })

        # ── Score each market ─────────────────────────────────────────────────
        signals = []

        for slug, data in by_market.items():
            yes_agents = data["yes"]
            no_agents  = data["no"]
            title      = data["title"]

            # Determine dominant side
            yes_weight = sum(a["weight"] for a in yes_agents)
            no_weight  = sum(a["weight"] for a in no_agents)

            if yes_weight > no_weight:
                dominant_side   = "YES"
                dominant_agents = yes_agents
                total_weight    = yes_weight
            elif no_weight > yes_weight:
                dominant_side   = "NO"
                dominant_agents = no_agents
                total_weight    = no_weight
            else:
                continue  # tied — skip

            # Need minimum agent count
            if len(dominant_agents) < min_agents:
                continue

            # Weighted average entry price
            avg_entry = sum(a["price"] * a["weight"] for a in dominant_agents) / total_weight

            # Get current Polymarket price
            current_price = get_polymarket_price(slug)
            if current_price is None:
                continue

            # Agents' bullish gap: avg_entry - current_price
            # Positive = agents paid more = they think it's worth more = bullish vs market
            if dominant_side == "YES":
                gap = avg_entry - current_price
            else:
                # For NO: agents' avg NO price vs current NO price (1-yes)
                current_no = 1.0 - current_price
                gap = avg_entry - current_no

            if gap < min_bullish_gap:
                continue

            # Whale confirmation (optional)
            whale_volume = whale_buys.get(slug, 0)
            has_whale_conf = whale_volume > 0

            if require_whale_conf and not has_whale_conf:
                continue

            # Build signal score
            score = 0
            score += min(len(dominant_agents), 5)       # up to 5 pts for agent count
            score += min(int(gap * 20), 4)              # up to 4 pts for gap size
            score += int(total_weight / len(dominant_agents) > 1.2)  # bonus for quality agents
            score += 2 if has_whale_conf else 0         # +2 for whale confirmation

            best_agent = max(dominant_agents, key=lambda a: a["roi"])

            signals.append({
                "slug": slug,
                "title": title,
                "dominant_side": dominant_side,
                "agent_count": len(dominant_agents),
                "avg_entry": avg_entry,
                "current_price": current_price,
                "gap": gap,
                "score": score,
                "has_whale_conf": has_whale_conf,
                "whale_volume": whale_volume,
                "best_agent": best_agent["agent"],
                "best_agent_roi": best_agent["roi"],
                "agents": [a["agent"] for a in dominant_agents],
            })

        if not signals:
            self.log.debug("No agent vs market signals found.")
            return

        signals.sort(key=lambda x: x["score"], reverse=True)
        self.log.info(f"Found {len(signals)} agent signals. Top: {signals[0]['title'][:50]}")

        # ── Match to Kalshi + execute ─────────────────────────────────────────
        for sig in signals[:5]:
            kalshi_match = match_kalshi_market(sig["title"], sig["slug"], markets)

            if not kalshi_match:
                self.log.debug(f"No Kalshi match: {sig['title'][:60]}")
                # Still alert — useful intel
                whale_line = f"\n🐳 Whale confirmation: ${sig['whale_volume']:,.0f}" if sig["has_whale_conf"] else ""
                notifier.send(
                    f"🤖 Agent Bullish Signal (no Kalshi match)\n"
                    f"{sig['title'][:80]}\n"
                    f"Side: {sig['dominant_side']} | {sig['agent_count']} agents\n"
                    f"Agents avg: {sig['avg_entry']:.0%} vs market: {sig['current_price']:.0%} "
                    f"(+{sig['gap']:.0%} gap){whale_line}\n"
                    f"🔗 https://polymarket.com/event/{sig['slug']}"
                )
                continue

            ticker   = kalshi_match.get("ticker", "")
            k_title  = kalshi_match.get("title", "")
            yes_ask  = kalshi_match.get("yes_ask", 0)
            no_ask   = kalshi_match.get("no_ask", 0)

            entry_cents = yes_ask if sig["dominant_side"] == "YES" else no_ask
            if entry_cents <= 0:
                continue
            if self.is_already_open(state, ticker):
                continue
            if not self.can_open(state, risk, max_pos):
                break

            contracts = api.usd_to_contracts(max_pos, entry_cents)
            url       = api.market_url(ticker)
            whale_line = f"\n🐳 Whale backing: ${sig['whale_volume']:,.0f}" if sig["has_whale_conf"] else ""

            detail = (
                f"Side: {sig['dominant_side']} | Score: {sig['score']}/11\n"
                f"{sig['agent_count']} agents agree | "
                f"Avg entry: {sig['avg_entry']:.0%} vs market: {sig['current_price']:.0%}\n"
                f"Bullish gap: +{sig['gap']:.0%} | Agents: {', '.join(sig['agents'][:4])}"
                f"{whale_line}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] Agent signal: "
                f"{sig['title'][:50]} | {sig['dominant_side']}@{entry_cents}¢ | score={sig['score']}"
            )
            notifier.opportunity_alert("🤖 Agent Signal", k_title[:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name,
                                        sig["dominant_side"], entry_cents, max_pos)
            else:
                api.place_order(ticker, sig["dominant_side"].lower(), contracts, entry_cents)
                state_mgr.open_position(state, ticker, self.name,
                                        sig["dominant_side"], entry_cents, max_pos)
