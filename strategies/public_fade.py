"""
Public Fade Strategy — Kalshi-first approach
=============================================
1. Fetch Kalshi game-winner markets (what's actually available to bet)
2. For each game, look up betting % data on Action Network
3. If public heavily on one side but sharp money NOT following → fade signal
4. Bet the fade side on Kalshi

This is Kalshi-first: we only look for AN data on games Kalshi actually offers,
so we never waste cycles finding signals with no market to trade.
"""
from datetime import datetime, timezone, timedelta
import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import TEAM_ALIASES
from core import api, notifier, state as state_mgr
from core.health import cached

# Fragments that reliably identify a sport from a Kalshi market title
MLB_FRAGMENTS = {
    "new york y", "new york m", "los angeles d", "los angeles a", "chicago c",
    "chicago w", "chicago ws", "boston", "red sox", "yankee", "dodger", "padre",
    "mariner", "athletics", "a's", "brewer", "twin", "brave", "cardinal",
    "pirate", "guardian", "tiger", "oriole", "ray", "royal", "astro", "ranger",
    "angel", "phillie", "diamondback", "rocki", "cub", "giant", "met",
    "cleveland", "tampa bay", "houston", "seattle", "san diego", "san francisco",
    "kansas city", "st. louis", "milwauke", "baltimor", "cincinnati", "colorado",
}
NBA_FRAGMENTS = {
    "timberwolve", "wolf", "knick", "laker", "celtics", "warrior", "heat",
    "buck", "sixer", "76er", "net", "bull", "cavalier", "raptor", "pacer",
    "hawk", "hornet", "magic", "piston", "wizard", "nugget", "thunder",
    "jazz", "blazer", "sun", "spur", "maverick", "rocket", "grizzl", "pelican",
    "king", "clipper",
}
NFL_FRAGMENTS = {
    "patriot", "bill", "dolphin", "ravens", "steeler", "brown", "chief",
    "raider", "charger", "bronco", "cowboy", "eagle", "commander", "bear",
    "viking", "lion", "packer", "seahawk", "49er", "ram", "cardinal",
    "falcon", "saint", "buccaneer", "panther", "jaguar", "titan", "colt",
    "texan",
}
NHL_FRAGMENTS = {
    "bruin", "sabre", "black", "hawk", "blackhawk", "jet", "mammoth",
    "capital", "penguin", "senator", "oiler", "flame", "canuck", "kraken",
    "shark", "king", "duck", "avalanche", "wild", "predator", "blue",
    "jacket", "lightning", "bolt", "panther", "devil", "ranger", "islander",
    "golden knight", "vgk", "canadien", "habs",
}

def infer_sport_from_title(title: str) -> str | None:
    t = title.lower()
    for frag in MLB_FRAGMENTS:
        if frag in t:
            return "MLB"
    for frag in NHL_FRAGMENTS:
        if frag in t:
            return "NHL"
    for frag in NBA_FRAGMENTS:
        if frag in t:
            return "NBA"
    for frag in NFL_FRAGMENTS:
        if frag in t:
            return "NFL"
    return None

AN_BASE = "https://api.actionnetwork.com/web/v1/scoreboard"
AN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept": "application/json",
}
CONSENSUS_BOOK_ID = 15
SPORTS = ["nba", "mlb", "nfl", "nhl", "ncaab", "wnba"]

_session = requests.Session()
_session.headers.update(AN_HEADERS)


# ── Action Network ────────────────────────────────────────────────────────────

def get_all_an_leans() -> list:
    """Fetch all live games + public % data across all sports."""
    def fetch():
        leans = []
        for sport in SPORTS:
            try:
                r = _session.get(f"{AN_BASE}/{sport}", timeout=10)
                r.raise_for_status()
                for game in r.json().get("games", []):
                    lean = _extract_lean(game, sport)
                    if lean:
                        leans.append(lean)
            except Exception:
                pass
        return leans
    return cached("all_an_leans", ttl=180, fetch_fn=fetch) or []


def _extract_lean(game: dict, sport: str) -> dict | None:
    teams = {t["id"]: t for t in game.get("teams", [])}
    home_id = game.get("home_team_id")
    away_id = game.get("away_team_id")
    if not home_id or not away_id:
        return None

    consensus = next((o for o in game.get("odds", [])
                      if o.get("book_id") == CONSENSUS_BOOK_ID), None)
    if not consensus:
        return None

    home_pub   = consensus.get("ml_home_public")
    away_pub   = consensus.get("ml_away_public")
    home_money = consensus.get("ml_home_money")
    away_money = consensus.get("ml_away_money")

    if home_pub is None or away_pub is None:
        return None

    return {
        "sport":       sport.upper(),
        "home_team":   teams.get(home_id, {}),
        "away_team":   teams.get(away_id, {}),
        "home_pub":    home_pub,
        "away_pub":    away_pub,
        "home_money":  home_money or 50,
        "away_money":  away_money or 50,
        "status":      game.get("status", ""),
        "start_time":  game.get("start_time", ""),
    }


# ── Kalshi ↔ Action Network matching ─────────────────────────────────────────

def _team_score(kalshi_frag: str, an_full_name: str) -> int:
    """Score how well a Kalshi team name fragment matches an AN full team name."""
    frag = kalshi_frag.lower().strip().rstrip("?")
    full = an_full_name.lower()
    score = 0

    # Direct substring
    if frag in full or full in frag:
        score += 4

    # Word overlap
    for word in frag.split():
        if len(word) > 2 and word in full:
            score += 1

    # Alias-based: find which TEAM_ALIASES entry matches the frag
    for team_key, aliases in TEAM_ALIASES.items():
        if any(alias in frag for alias in aliases) or any(alias == frag for alias in aliases):
            # team_key maps to this frag — does the AN name match?
            if any(alias in full for alias in aliases) or team_key.split()[0] in full:
                score += 3
            break

    return score


def build_team_signals(all_leans: list) -> dict:
    """
    Build a team-level signal map from AN data.
    Key: lowercased full team name
    Value: {pub_pct, money_pct, sport, opponent}
    Public bias on a team is relatively stable regardless of specific opponent.
    """
    signals = {}
    for lean in all_leans:
        for side in ("home", "away"):
            team = lean[f"{side}_team"]
            name = team.get("full_name", "").lower()
            if not name:
                continue
            opp_side = "away" if side == "home" else "home"
            signals[name] = {
                "pub_pct":   lean[f"{side}_pub"],
                "money_pct": lean[f"{side}_money"],
                "sport":     lean["sport"],
                "opponent":  lean[f"{opp_side}_team"].get("full_name", ""),
            }
    return signals


def find_team_signal(kalshi_frag: str, team_signals: dict) -> tuple[str | None, dict | None]:
    """
    Given a Kalshi team name fragment (e.g. 'San Diego', 'Los Angeles D'),
    find the best matching team in the signal map.
    Returns (matched_full_name, signal_dict) or (None, None).
    """
    best_name, best_sig, best_score = None, None, 0
    for full_name, sig in team_signals.items():
        score = _team_score(kalshi_frag, full_name)
        if score > best_score:
            best_score = score
            best_name = full_name
            best_sig  = sig
    return (best_name, best_sig) if best_score >= 3 else (None, None)


# ── Strategy ──────────────────────────────────────────────────────────────────

class PublicFadeStrategy(BaseStrategy):
    name = "public_fade"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        min_pub_pct    = cfg.get("min_public_pct", 65)
        min_divergence = cfg.get("min_divergence", 15)
        max_money_pct  = cfg.get("max_money_pct", 85)
        max_pos        = cfg.get("max_position_usd", 250)
        max_hours      = cfg.get("max_hours_to_close", 48)
        risk           = state.get("_risk_config", {})

        now     = datetime.now(timezone.utc)
        cutoff  = now + timedelta(hours=max_hours)

        # ── Step 1: Fetch AN data + build team-level signal map ──────────────
        all_leans = get_all_an_leans()
        if not all_leans:
            self.log.warning("Could not fetch Action Network data.")
            return
        team_signals = build_team_signals(all_leans)

        # ── Step 2: Filter Kalshi game-winner markets ─────────────────────────
        game_markets = []
        for m in markets:
            if m.get("_category", "").lower() != "sports":
                continue
            title = m.get("title", "")
            if not any(kw in title.lower() for kw in ("winner", "vs")):
                continue
            ct = m.get("close_time") or m.get("expiration_time", "")
            if not ct:
                continue
            try:
                closes = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if closes <= now or closes > cutoff:
                    continue
            except Exception:
                continue
            if self.is_already_open(state, m.get("ticker", "")):
                continue
            game_markets.append(m)

        if not game_markets:
            self.log.info("No Kalshi game-winner markets closing within window.")
            return

        self.log.info(
            f"Checking {len(game_markets)} Kalshi game markets against "
            f"{len(all_leans)} AN games for fade signal."
        )

        # ── Step 3: For each Kalshi market, look up both teams in AN signals ──
        trades = []
        for km in game_markets:
            ticker  = km.get("ticker", "")
            title   = km.get("title", "")
            yes_ask = km.get("yes_ask", 0)
            no_ask  = km.get("no_ask", 0)

            if " vs " not in title.lower():
                continue

            parts   = title.lower().split(" vs ", 1)
            frag_yes = parts[0].strip()                               # YES = this team wins
            frag_no  = parts[1].replace("winner?","").replace("winner","").strip()

            # Look up each team's public betting signal
            yes_name, yes_sig = find_team_signal(frag_yes, team_signals)
            no_name,  no_sig  = find_team_signal(frag_no,  team_signals)

            if not yes_sig and not no_sig:
                continue

            # Check if either team has a strong public lean → signal to fade them
            fade_side = None
            for side, name, sig, opp_sig in [
                ("NO",  yes_name, yes_sig, no_sig),   # public on YES team → fade = NO
                ("YES", no_name,  no_sig,  yes_sig),  # public on NO team  → fade = YES
            ]:
                if not sig:
                    continue
                pub_pct   = sig["pub_pct"]
                money_pct = sig["money_pct"]
                divergence = pub_pct - money_pct

                if pub_pct < min_pub_pct:
                    continue
                if divergence < min_divergence:
                    continue
                if money_pct >= max_money_pct:
                    continue

                fade_side = side
                public_name = name
                fade_name   = (no_name if side == "NO" else yes_name) or "?"
                entry_cents = no_ask if side == "NO" else yes_ask
                sport       = sig["sport"]
                break

            if not fade_side or not entry_cents:
                continue

            # Sport consistency: don't apply NBA signal to MLB market etc.
            inferred_sport = infer_sport_from_title(title)
            if inferred_sport and inferred_sport != sport:
                self.log.debug(
                    f"Sport mismatch: Kalshi title suggests {inferred_sport} "
                    f"but signal is from {sport} — skipping {title[:50]}"
                )
                continue

            expected_ret = round((100 - entry_cents) / entry_cents * 100, 1)

            trades.append({
                "ticker":       ticker,
                "title":        title,
                "kalshi_side":  fade_side,
                "entry_cents":  entry_cents,
                "public_name":  public_name,
                "fade_name":    fade_name,
                "pub_pct":      pub_pct,
                "money_pct":    money_pct,
                "divergence":   divergence,
                "sport":        sport,
                "expected_ret": expected_ret,
            })

            self.log.info(
                f"✅ [{sport}] {title[:55]} | "
                f"Public {pub_pct}% bets / {money_pct}% money on {public_name} "
                f"(+{divergence:.0f}pt) → fade {fade_name} {fade_side}@{entry_cents}¢"
            )

        if not trades:
            self.log.info("No fade signals found matching Kalshi markets this tick.")
            return

        # Sort by divergence (strongest signal first)
        trades.sort(key=lambda x: x["divergence"], reverse=True)

        # ── Step 4: Execute ───────────────────────────────────────────────────
        for t in trades:
            if not self.can_open(state, risk, max_pos, cfg):
                break

            contracts = api.usd_to_contracts(max_pos, t["entry_cents"])
            url       = self.market_url(t["ticker"])

            detail = (
                f"📊 Bets: {t['pub_pct']}% on {t['public_name']}\n"
                f"💰 Money: {t['money_pct']}% on {t['public_name']}\n"
                f"⚡ Divergence: +{t['divergence']:.0f}pt — sharp money not following\n"
                f"📉 Fade: {t['kalshi_side']} on {t['fade_name']} @ {t['entry_cents']}¢\n"
                f"Expected: +{t['expected_ret']:.1f}% | Size: ${max_pos}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] "
                f"[{t['sport']}] FADE {t['fade_name']} {t['kalshi_side']}@{t['entry_cents']}¢ | "
                f"+{t['divergence']:.0f}pt divergence | +{t['expected_ret']:.1f}% if right"
            )
            notifier.opportunity_alert(
                f"📉 Fade [{t['sport']}]",
                t["title"][:80],
                detail, url
            )

            if paper_trading:
                state_mgr.open_position(
                    state, t["ticker"], self.name,
                    t["kalshi_side"], t["entry_cents"], max_pos
                )
            else:
                order = api.place_order(
                    t["ticker"], t["kalshi_side"].lower(),
                    contracts, t["entry_cents"]
                )
                if order is not None:
                    state_mgr.open_position(
                        state, t["ticker"], self.name,
                        t["kalshi_side"], t["entry_cents"], max_pos
                    )
