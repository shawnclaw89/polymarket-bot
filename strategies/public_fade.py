"""
Public Fade Strategy
====================
Primary signal: The Action Network public betting percentage data
Smart money confirmation: Polymarket whale activity on the fade side

Logic:
  1. Fetch live games from Action Network for NBA, MLB, NFL, NHL
  2. Find games where 65%+ of PUBLIC BETS are on one team (the "public side")
  3. Check Polymarket whale_trades_cache: did smart money recently buy the OTHER team?
  4. If confirmed → fade the public on Kalshi (buy the underdog side)
  5. Entry: only if Kalshi market exists and underdog price is 28–72¢

This uses REAL betting percentage data, not price as a proxy for public action.
A team being a Kalshi favorite doesn't mean public is on them — this does.
"""
import time
import os
from datetime import datetime, timezone, timedelta
import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import (
    SUPABASE_URL, ANON_KEY, HEADERS,
    get_trader_stats,
    extract_team_tokens, TEAM_ALIASES,
)
from core import api, notifier, state as state_mgr
from core.health import cached

AN_BASE = "https://api.actionnetwork.com/web/v1/scoreboard"
AN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept": "application/json",
}
CONSENSUS_BOOK_ID = 15   # Action Network consensus aggregate

SPORTS = ["nba", "mlb", "nfl", "nhl"]  # leagues to scan

_an_session = requests.Session()
_an_session.headers.update(AN_HEADERS)

_pm_session = requests.Session()
_pm_session.headers.update({"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"})


# ── Action Network helpers ────────────────────────────────────────────────────

def get_action_network_games(sport: str) -> list:
    """Fetch today's games for a sport from Action Network."""
    def fetch():
        try:
            r = _an_session.get(f"{AN_BASE}/{sport}", timeout=10)
            r.raise_for_status()
            return r.json().get("games", [])
        except Exception as e:
            return []
    return cached(f"an_games_{sport}", ttl=180, fetch_fn=fetch) or []


def get_public_lean(game: dict) -> dict | None:
    """
    Extract consensus public betting data from a game.
    Returns dict with fade info, or None if no strong lean found.
    """
    teams = {t["id"]: t for t in game.get("teams", [])}
    home_id = game.get("home_team_id")
    away_id = game.get("away_team_id")

    if not home_id or not away_id:
        return None

    home_team = teams.get(home_id, {})
    away_team = teams.get(away_id, {})

    # Find consensus odds entry
    consensus = None
    for o in game.get("odds", []):
        if o.get("book_id") == CONSENSUS_BOOK_ID:
            consensus = o
            break

    if not consensus:
        return None

    home_pub = consensus.get("ml_home_public")
    away_pub = consensus.get("ml_away_public")
    home_money = consensus.get("ml_home_money")
    away_money = consensus.get("ml_away_money")
    ml_home = consensus.get("ml_home")
    ml_away = consensus.get("ml_away")

    if home_pub is None or away_pub is None:
        return None

    return {
        "home_team":    home_team,
        "away_team":    away_team,
        "home_pub_pct": home_pub,
        "away_pub_pct": away_pub,
        "home_money_pct": home_money,
        "away_money_pct": away_money,
        "ml_home":      ml_home,   # moneyline odds (e.g. -150, +130)
        "ml_away":      ml_away,
        "start_time":   game.get("start_time", ""),
        "status":       game.get("status", ""),
        "game_id":      game.get("id"),
    }


# ── Polymarket whale confirmation ─────────────────────────────────────────────

def get_whale_trades_for_underdog(underdog_name: str, lookback_hours: int,
                                  min_usd: float) -> list:
    """
    Search PolymarketScan for recent whale trades on the underdog side.
    Underdog = price < 0.55 AND the team name appears in the market title.
    """
    def fetch():
        try:
            r = _pm_session.get(
                f"{SUPABASE_URL}/whale_trades_cache",
                params={
                    "select": "tx_hash,wallet,market_title,market_slug,side,"
                              "outcome,amount_usd,price,timestamp,tier,anomaly_tags",
                    "amount_usd": f"gte.{min_usd}",
                    "order": "timestamp.desc",
                    "limit": 100,
                }, timeout=10,
            )
            r.raise_for_status()
            return r.json() if isinstance(r.json(), list) else []
        except Exception:
            return []

    all_trades = cached("whale_trades_fade_v2", ttl=120, fetch_fn=fetch) or []

    now_ts = time.time()
    cutoff = now_ts - (lookback_hours * 3600)

    # Build search tokens from underdog name + aliases
    search_tokens = [underdog_name.lower()]
    for team_key, aliases in TEAM_ALIASES.items():
        if team_key in underdog_name.lower() or underdog_name.lower() in team_key:
            search_tokens.extend(aliases)

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

        # Only BUY trades
        if t.get("side", "").upper() != "BUY":
            continue

        # Smart money price filter: underdog = price < 0.55
        price = t.get("price", 1.0)
        if price >= 0.55:
            continue

        # Team name must appear in market title
        pm_title = (t.get("market_title") or "").lower()
        if any(tok in pm_title for tok in search_tokens):
            matching.append(t)

    return matching


# ── Kalshi matching helpers ───────────────────────────────────────────────────

def find_kalshi_game(team_a: str, team_b: str, markets: list) -> dict | None:
    """
    Find a Kalshi game-winner market for the given matchup.
    Returns the best match or None.
    """
    tokens_a = extract_team_tokens(team_a)
    tokens_b = extract_team_tokens(team_b)

    best, best_score = None, 0
    for km in markets:
        if km.get("_category", "").lower() != "sports":
            continue
        title = (km.get("title") or "").lower()
        if not any(kw in title for kw in ("winner", "win", "vs")):
            continue

        hits_a = 1 if tokens_a and any(a in title for aliases in tokens_a for a in aliases) else 0
        hits_b = 1 if tokens_b and any(a in title for aliases in tokens_b for a in aliases) else 0
        score = hits_a + hits_b
        if score > best_score:
            best_score = score
            best = km

    return best if best_score >= 2 else None


def determine_kalshi_side(kalshi_title: str, fade_team_name: str) -> str:
    """
    Determine if the fade team is on the YES or NO side of a Kalshi market.
    Kalshi "X vs Y Winner?" convention: YES = X wins, NO = Y wins.
    """
    title_lower = kalshi_title.lower()

    # Get aliases for the fade team
    fade_tokens = []
    for team_key, aliases in TEAM_ALIASES.items():
        if team_key in fade_team_name.lower() or fade_team_name.lower() in team_key:
            fade_tokens.extend(aliases)
    fade_tokens.append(fade_team_name.lower())

    # Split on " vs "
    if " vs " in title_lower:
        yes_half = title_lower.split(" vs ")[0]
        no_half  = title_lower.split(" vs ")[1]
        if any(tok in yes_half for tok in fade_tokens):
            return "YES"
        if any(tok in no_half for tok in fade_tokens):
            return "NO"

    # Fallback: check full title
    if any(tok in title_lower for tok in fade_tokens):
        # Can't determine side from title alone — use price as tiebreaker
        return "UNKNOWN"

    return "UNKNOWN"


# ── Strategy ──────────────────────────────────────────────────────────────────

class PublicFadeStrategy(BaseStrategy):
    name = "public_fade"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        min_pub_pct      = cfg.get("min_public_pct", 65)      # % of bets on public side to trigger
        min_money_pct    = cfg.get("min_money_pct", 55)        # % of money (looser — sharp money can offset)
        max_pos          = cfg.get("max_position_usd", 250)
        max_hours        = cfg.get("max_hours_to_close", 48)
        lookback_hours   = cfg.get("whale_lookback_hours", 6)
        min_whale_usd    = cfg.get("min_whale_trade_usd", 200)
        min_whale_trades = cfg.get("min_whale_confirmations", 1)
        max_entry        = cfg.get("max_entry_cents", 72)      # don't buy underdog above this
        min_entry        = cfg.get("min_entry_cents", 28)      # don't buy extreme longshots
        risk             = state.get("_risk_config", {})

        now = datetime.now(timezone.utc)
        game_cutoff = now + timedelta(hours=max_hours)

        fade_opps = []

        # ── Step 1: Scan Action Network for public-heavy games ────────────────
        for sport in SPORTS:
            games = get_action_network_games(sport)
            for game in games:
                lean = get_public_lean(game)
                if not lean:
                    continue

                # Skip games not starting within our window
                start_str = lean["start_time"]
                try:
                    start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    if start_time > game_cutoff:
                        continue
                except Exception:
                    pass

                # Skip finished games
                if lean["status"] in ("complete", "closed", "final"):
                    continue

                # Find which side the public is heavily on
                h_pub = lean["home_pub_pct"]
                a_pub = lean["away_pub_pct"]
                h_money = lean.get("home_money_pct") or 50
                a_money = lean.get("away_money_pct") or 50

                if h_pub >= min_pub_pct:
                    public_team  = lean["home_team"]
                    fade_team    = lean["away_team"]
                    pub_pct      = h_pub
                    money_pct    = h_money
                elif a_pub >= min_pub_pct:
                    public_team  = lean["away_team"]
                    fade_team    = lean["home_team"]
                    pub_pct      = a_pub
                    money_pct    = a_money
                else:
                    continue  # no strong public lean

                # Money check: if sharp money is ALSO with public, skip
                # (we want public money pct to be high but not reinforced by sharp money)
                if money_pct is not None and money_pct >= 75:
                    self.log.debug(
                        f"Skipping {fade_team.get('full_name')} fade — money also with public "
                        f"({money_pct}%)"
                    )
                    continue

                fade_opps.append({
                    "sport":       sport.upper(),
                    "public_team": public_team,
                    "fade_team":   fade_team,
                    "pub_pct":     pub_pct,
                    "money_pct":   money_pct,
                    "lean":        lean,
                })

        if not fade_opps:
            self.log.info("No public-heavy games found across NBA/MLB/NFL/NHL this tick.")
            return

        self.log.info(
            f"Found {len(fade_opps)} public-heavy game(s) to check for whale confirmation."
        )
        for opp in fade_opps:
            self.log.info(
                f"  [{opp['sport']}] Public {opp['pub_pct']}% on "
                f"{opp['public_team'].get('full_name')} → fade "
                f"{opp['fade_team'].get('full_name')} "
                f"(money: {opp['money_pct']}% with public)"
            )

        # ── Step 2: Whale confirmation on fade side ───────────────────────────
        confirmed = []
        for opp in fade_opps:
            fade_name = opp["fade_team"].get("full_name", "")

            whale_confirms = get_whale_trades_for_underdog(
                fade_name,
                lookback_hours=lookback_hours,
                min_usd=min_whale_usd,
            )

            if len(whale_confirms) < min_whale_trades:
                self.log.info(
                    f"  ❌ No whale on {fade_name} "
                    f"({len(whale_confirms)}/{min_whale_trades} required)"
                )
                continue

            total_whale_usd = sum(t.get("amount_usd", 0) for t in whale_confirms)
            top_whale = whale_confirms[0]
            stats = get_trader_stats(top_whale.get("wallet", ""))
            trader_name = stats.get("display_name") or top_whale.get("wallet", "")[:10] + "..."
            trader_pnl  = stats.get("total_pnl", 0)
            trader_wr   = stats.get("win_rate", 0)

            opp["whale_confirms"]  = len(whale_confirms)
            opp["whale_usd"]       = total_whale_usd
            opp["trader_name"]     = trader_name
            opp["trader_pnl"]      = trader_pnl
            opp["trader_wr"]       = trader_wr
            opp["pm_price"]        = top_whale.get("price", 0)
            confirmed.append(opp)

            self.log.info(
                f"  ✅ Whale confirmed: {fade_name} | "
                f"{len(whale_confirms)} whale(s) | ${total_whale_usd:,.0f} | "
                f"{trader_name} WR={trader_wr:.0f}%"
            )

        if not confirmed:
            self.log.info("No confirmed fade opportunities this tick.")
            return

        # Sort: most public skew first
        confirmed.sort(key=lambda x: x["pub_pct"], reverse=True)

        # ── Step 3: Match to Kalshi + execute ────────────────────────────────
        for opp in confirmed[:3]:
            if not self.can_open(state, risk, max_pos, cfg):
                break

            public_name = opp["public_team"].get("full_name", "")
            fade_name   = opp["fade_team"].get("full_name", "")

            # Find Kalshi market
            kalshi_match = find_kalshi_game(public_name, fade_name, markets)
            if not kalshi_match:
                self.log.info(f"  No Kalshi market found for: {public_name} vs {fade_name}")
                continue

            ticker = kalshi_match.get("ticker", "")
            title  = kalshi_match.get("title", "")
            yes_ask = kalshi_match.get("yes_ask", 0)
            no_ask  = kalshi_match.get("no_ask", 0)

            if self.is_already_open(state, ticker):
                continue

            # Determine which Kalshi side = fade team
            side = determine_kalshi_side(title, fade_name)
            if side == "UNKNOWN":
                # Fall back: lower price = underdog = fade side
                side = "YES" if yes_ask <= no_ask else "NO"

            entry_cents = yes_ask if side == "YES" else no_ask

            if not (min_entry <= entry_cents <= max_entry):
                self.log.info(
                    f"  Kalshi entry {entry_cents}¢ outside range "
                    f"[{min_entry}–{max_entry}¢] for {fade_name}"
                )
                continue

            contracts = api.usd_to_contracts(max_pos, entry_cents)
            url = self.market_url(ticker)
            expected_ret = round((100 - entry_cents) / entry_cents * 100, 1)

            detail = (
                f"📊 Public: {opp['pub_pct']}% bets on {public_name} | "
                f"money: {opp['money_pct']}%\n"
                f"📉 Fading with {side} on {fade_name} @ {entry_cents}¢\n"
                f"Expected return: +{expected_ret:.1f}%\n"
                f"🐋 {opp['whale_confirms']} whale(s) on fade side — "
                f"${opp['whale_usd']:,.0f} on Polymarket\n"
                f"Top: {opp['trader_name']} | "
                f"PnL ${opp['trader_pnl']:,.0f} | WR {opp['trader_wr']:.0f}%"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] FADE [{opp['sport']}] "
                f"{fade_name} {side}@{entry_cents}¢ | "
                f"public {opp['pub_pct']}% on {public_name} | "
                f"+{expected_ret:.1f}% if right"
            )
            notifier.opportunity_alert(
                f"📉 Public Fade [{opp['sport']}]",
                f"{public_name} vs {fade_name}",
                detail, url
            )

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name,
                                        side, entry_cents, max_pos)
            else:
                order = api.place_order(ticker, side.lower(), contracts, entry_cents)
                if order is not None:
                    state_mgr.open_position(state, ticker, self.name,
                                            side, entry_cents, max_pos)
