"""
Public Fade Strategy
====================
Signal: The Action Network public betting percentage data

Logic:
  1. Fetch live games from Action Network for NBA, MLB, NFL, NHL
  2. Find games where PUBLIC BETS ≥ min_public_pct on one team
  3. Check that MONEY % is NOT equally high (sharp money not confirming public)
     — bets/money divergence is the signal: public piling in, sharp sitting out
  4. Fade the public on Kalshi (buy the underdog side)
  5. Entry: Kalshi market exists, underdog price is 28–72¢

No Polymarket dependency. Action Network public % data is the sole signal.
"""
import time
from datetime import datetime, timezone, timedelta
import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import extract_team_tokens, TEAM_ALIASES
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
        min_pub_pct    = cfg.get("min_public_pct", 65)   # % of bets on public side to trigger
        min_divergence = cfg.get("min_divergence", 15)   # min gap: bets% - money% — the signal
        max_money_pct  = cfg.get("max_money_pct", 85)    # skip if sharp money also heavily with public
        max_pos        = cfg.get("max_position_usd", 250)
        max_hours      = cfg.get("max_hours_to_close", 48)
        risk           = state.get("_risk_config", {})

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

                # The signal: bets% >> money% means public is piling in but sharp money isn't
                # Calculate divergence: positive = public heavy, sharp sitting out
                divergence = pub_pct - (money_pct or 50)

                # Skip if divergence too small (sharp money tracking public)
                if divergence < min_divergence:
                    self.log.debug(
                        f"Skipping {fade_team.get('full_name')} — "
                        f"divergence only {divergence:.0f}pt ({pub_pct}% bets / {money_pct}% money)"
                    )
                    continue

                # Hard ceiling: if money% also very high, sharp confirms public — no fade
                if money_pct is not None and money_pct >= max_money_pct:
                    self.log.debug(
                        f"Skipping {fade_team.get('full_name')} — "
                        f"sharp money also heavily with public ({money_pct}% ≥ {max_money_pct}%)"
                    )
                    continue

                fade_opps.append({
                    "sport":       sport.upper(),
                    "public_team": public_team,
                    "fade_team":   fade_team,
                    "pub_pct":     pub_pct,
                    "money_pct":   money_pct,
                    "divergence":  divergence,
                    "lean":        lean,
                })

        if not fade_opps:
            self.log.info("No public-heavy games found across NBA/MLB/NFL/NHL this tick.")
            return

        self.log.info(f"Found {len(fade_opps)} fade opportunity(s):")
        for opp in fade_opps:
            self.log.info(
                f"  [{opp['sport']}] {opp['pub_pct']}% bets / {opp['money_pct']}% money "
                f"on {opp['public_team'].get('full_name')} "
                f"(+{opp['divergence']:.0f}pt gap) → fade {opp['fade_team'].get('full_name')}"
            )

        # Sort: biggest bets/money divergence first — that's the strength of the signal
        fade_opps.sort(key=lambda x: x["divergence"], reverse=True)

        # ── Step 2: Match to Kalshi + execute ────────────────────────────────
        for opp in fade_opps[:5]:
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

            if entry_cents <= 0:
                continue

            contracts = api.usd_to_contracts(max_pos, entry_cents)
            url = self.market_url(ticker)
            expected_ret = round((100 - entry_cents) / entry_cents * 100, 1)

            detail = (
                f"📊 Bets: {opp['pub_pct']}% on {public_name}\n"
                f"💰 Money: {opp['money_pct']}% on {public_name}\n"
                f"⚡ Divergence: +{opp['divergence']:.0f}pt — sharp money NOT following public\n"
                f"📉 Fade: {side} on {fade_name} @ {entry_cents}¢\n"
                f"Expected return: +{expected_ret:.1f}% | Size: ${max_pos}"
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
