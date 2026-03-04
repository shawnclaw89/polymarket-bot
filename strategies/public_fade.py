"""
Public Fade Strategy — requires user approval before any trade fires
====================================================================
Flow:
  1. Fetch Kalshi game-winner markets
  2. For each, look up Action Network betting % by team name
  3. Signal = public % >> money % on same team (divergence ≥ threshold)
  4. Validate sport match — no NBA signals on MLB markets, etc.
  5. Queue signal in pending_trades.json, send Telegram alert
  6. On next tick, if user approved → execute. Otherwise skip.

Fixes vs previous version:
  - Game deduplication (one trade per game, never both sides)
  - Sport inference validation
  - No auto-fire in live mode — all trades require approval
"""
import uuid
from datetime import datetime, timezone, timedelta
import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import TEAM_ALIASES
from core import api, notifier, state as state_mgr, pending as pending_mgr
from core.health import cached

AN_BASE          = "https://api.actionnetwork.com/web/v1/scoreboard"
AN_HEADERS       = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept":     "application/json",
}
CONSENSUS_BOOK_ID = 15
SPORTS = ["nba", "mlb", "nfl", "nhl", "ncaab"]

_session = requests.Session()
_session.headers.update(AN_HEADERS)

# ── Sport inference from Kalshi title ─────────────────────────────────────────
# Fragments that reliably identify which sport a Kalshi market belongs to.
# Order matters — check MLB before NHL (both have "boston", "buffalo" etc.)
_SPORT_FRAGS = [
    ("MLB", {
        "new york y", "new york m", "los angeles d", "los angeles a",
        "chicago c", "chicago w", "chicago ws",
        "yankee", "dodger", "padre", "mariner", "mets",
        "athletics", "a's", "brewer", "twin", "brave", "cardinal",
        "pirate", "guardian", "tiger", "oriole", "ray", "royal",
        "astro", "ranger", "angel", "phillie", "diamondback",
        "rocki", "cub", "giant", "red sox",
        "cleveland", "tampa bay", "houston", "seattle", "san diego",
        "san francisco", "kansas city", "st. louis", "milwauke",
        "baltimor", "cincinnati", "colorado",
    }),
    ("NHL", {
        "bruin", "sabre", "blackhawk", "jet", "mammoth",
        "capital", "penguin", "senator", "oiler", "flame", "canuck",
        "kraken", "shark", "duck", "avalanche", "wild", "predator",
        "lightning", "panther", "devil", "golden knight", "canadien",
        "habs", "blue jacket", "islander",
        # City names for teams whose Kalshi title uses city only
        "ottawa", "edmonton", "winnipeg", "calgary", "vancouver",
        "buffalo", "nashville", "columbus", "pittsburgh", "new jersey",
        "montreal", "winnipeg", "utah mammoth", "vegas",
    }),
    ("NBA", {
        "timberwolve", "wolf", "knick", "laker", "celtics", "warrior",
        "heat", "buck", "sixer", "76er", "net", "bull", "cavalier",
        "raptor", "pacer", "hawk", "hornet", "magic", "piston",
        "wizard", "nugget", "thunder", "jazz", "blazer", "sun",
        "spur", "maverick", "rocket", "grizzl", "pelican", "clipper",
        "oklahoma city", "san antonio", "new orleans", "golden state",
        "portland", "sacramento", "memphis", "denver", "indiana",
        "charlotte", "orlando", "detroit", "washington wizard",
    }),
    ("NFL", {
        "patriot", "dolphin", "ravens", "steeler", "chief", "raider",
        "charger", "bronco", "cowboy", "eagle", "commander", "viking",
        "packer", "seahawk", "49er", "falcon", "saint", "buccaneer",
        "panther", "jaguar", "titan", "colt", "texan",
    }),
]

def infer_sport(title: str) -> str | None:
    # Strip "winner?" suffix before checking so "chicago winner?" doesn't match "chicago w"
    t = title.lower().replace("winner?", "").replace("winner", "").strip()
    for sport, frags in _SPORT_FRAGS:
        if any(f in t for f in frags):
            return sport
    return None


def game_base_id(ticker: str) -> str:
    """Strip the team suffix from a Kalshi ticker to get the game ID.
    KXMLBSTGAME-26MAR051305MINNYY-NYY  →  KXMLBSTGAME-26MAR051305MINNYY
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else ticker


# ── Action Network ────────────────────────────────────────────────────────────

def get_all_an_leans() -> list:
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
    teams  = {t["id"]: t for t in game.get("teams", [])}
    home_id = game.get("home_team_id")
    away_id = game.get("away_team_id")
    if not home_id or not away_id:
        return None
    consensus = next(
        (o for o in game.get("odds", []) if o.get("book_id") == CONSENSUS_BOOK_ID), None
    )
    if not consensus:
        return None
    home_pub   = consensus.get("ml_home_public")
    away_pub   = consensus.get("ml_away_public")
    if home_pub is None or away_pub is None:
        return None
    return {
        "sport":      sport.upper(),
        "home_team":  teams.get(home_id, {}),
        "away_team":  teams.get(away_id, {}),
        "home_pub":   home_pub,
        "away_pub":   away_pub,
        "home_money": consensus.get("ml_home_money") or 50,
        "away_money": consensus.get("ml_away_money") or 50,
    }


# ── Team matching ─────────────────────────────────────────────────────────────

def _team_score(kalshi_frag: str, an_full_name: str) -> int:
    frag = kalshi_frag.lower().strip().rstrip("?")
    full = an_full_name.lower()
    score = 0
    if frag in full or full in frag:
        score += 4
    for word in frag.split():
        if len(word) > 2 and word in full:
            score += 1
    for team_key, aliases in TEAM_ALIASES.items():
        if any(alias in frag for alias in aliases) or any(alias == frag for alias in aliases):
            if any(alias in full for alias in aliases) or team_key.split()[0] in full:
                score += 3
            break
    return score


def build_team_signals(leans: list) -> dict:
    signals = {}
    for lean in leans:
        for side in ("home", "away"):
            name = lean[f"{side}_team"].get("full_name", "").lower()
            if not name:
                continue
            opp = "away" if side == "home" else "home"
            signals[name] = {
                "pub_pct":   lean[f"{side}_pub"],
                "money_pct": lean[f"{side}_money"],
                "sport":     lean["sport"],
                "opponent":  lean[f"{opp}_team"].get("full_name", ""),
            }
    return signals


def find_team_signal(frag: str, signals: dict) -> tuple:
    best_name, best_sig, best_score = None, None, 0
    for name, sig in signals.items():
        score = _team_score(frag, name)
        if score > best_score:
            best_score, best_name, best_sig = score, name, sig
    return (best_name, best_sig) if best_score >= 3 else (None, None)


# ── Strategy ──────────────────────────────────────────────────────────────────

class PublicFadeStrategy(BaseStrategy):
    name = "public_fade"

    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        min_pub_pct    = cfg.get("min_public_pct", 65)
        min_divergence = cfg.get("min_divergence", 15)
        max_money_pct  = cfg.get("max_money_pct", 85)
        max_pos        = cfg.get("max_position_usd", 250)
        max_hours      = cfg.get("max_hours_to_close", 168)
        risk           = state.get("_risk_config", {})
        telegram_to    = state.get("_telegram_to", "7591705971")

        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=max_hours)

        # ── Step 0: Execute any user-approved trades from previous ticks ──────
        self._execute_approved(state, risk, max_pos, cfg, paper_trading, telegram_to)

        # ── Step 1: Build AN team signal map ─────────────────────────────────
        all_leans = get_all_an_leans()
        if not all_leans:
            self.log.warning("Could not fetch Action Network data.")
            return
        team_signals = build_team_signals(all_leans)

        # ── Step 2: Kalshi game-winner markets ────────────────────────────────
        seen_games = set()   # dedup by game base ticker
        game_markets = []
        for m in markets:
            if m.get("_category", "").lower() != "sports":
                continue
            title = m.get("title", "")
            if " vs " not in title.lower():
                continue
            if "winner" not in title.lower():
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
            ticker  = m.get("ticker", "")
            game_id = game_base_id(ticker)
            if game_id in seen_games:
                continue   # already have a market for this game
            if self.is_already_open(state, ticker):
                continue
            if pending_mgr.is_game_already_queued(game_id):
                continue
            seen_games.add(game_id)
            game_markets.append(m)

        if not game_markets:
            self.log.info("No new Kalshi game markets to check.")
            return

        self.log.info(
            f"Checking {len(game_markets)} unique Kalshi games vs "
            f"{len(all_leans)} AN games."
        )

        # ── Step 3: Match & check signal ─────────────────────────────────────
        for km in game_markets:
            ticker  = km.get("ticker", "")
            title   = km.get("title", "")
            yes_ask = km.get("yes_ask", 0)
            no_ask  = km.get("no_ask", 0)

            parts    = title.lower().split(" vs ", 1)
            frag_yes = parts[0].strip()
            frag_no  = parts[1].replace("winner?","").replace("winner","").strip()

            yes_name, yes_sig = find_team_signal(frag_yes, team_signals)
            no_name,  no_sig  = find_team_signal(frag_no,  team_signals)

            if not yes_sig and not no_sig:
                continue

            # Validate sport — must agree between Kalshi title and AN data
            inferred_sport = infer_sport(title)

            signal_found = False
            for fade_side, pub_name, sig, entry_cents in [
                ("NO",  yes_name, yes_sig, no_ask),   # public on YES → fade = NO
                ("YES", no_name,  no_sig,  yes_ask),  # public on NO  → fade = YES
            ]:
                if not sig or not entry_cents:
                    continue

                # Sport consistency check
                if inferred_sport and sig["sport"] != inferred_sport:
                    self.log.debug(
                        f"Sport mismatch: title={inferred_sport} signal={sig['sport']} "
                        f"— skipping {title[:50]}"
                    )
                    continue

                pub_pct    = sig["pub_pct"]
                money_pct  = sig["money_pct"]
                divergence = pub_pct - money_pct

                if pub_pct < min_pub_pct:
                    continue
                if divergence < min_divergence:
                    continue
                if money_pct >= max_money_pct:
                    continue

                fade_name    = (no_name if fade_side == "NO" else yes_name) or "opponent"
                expected_ret = round((100 - entry_cents) / entry_cents * 100, 1)
                trade_id     = uuid.uuid4().hex[:6].upper()

                signal_summary = (
                    f"[{sig['sport']}] Public {pub_pct}% bets / {money_pct}% money "
                    f"on {pub_name} (+{divergence:.0f}pt)"
                )

                queued = pending_mgr.add(trade_id, {
                    "ticker":      ticker,
                    "kalshi_side": fade_side,
                    "entry_cents": entry_cents,
                    "size_usd":    max_pos,
                    "title":       title,
                    "signal":      signal_summary,
                    "sport":       sig["sport"],
                })

                if queued:
                    self.log.info(
                        f"⏳ Signal queued [{trade_id}]: {title[:55]} | "
                        f"{signal_summary} → fade {fade_name} {fade_side}@{entry_cents}¢"
                    )
                    notifier.send(
                        f"🔔 Fade Signal — Awaiting Approval\n\n"
                        f"ID: {trade_id}\n"
                        f"Game: {title}\n"
                        f"Signal: {signal_summary}\n"
                        f"Trade: BUY {fade_side} on {fade_name} @ {entry_cents}¢\n"
                        f"Expected: +{expected_ret:.1f}% | Size: ${max_pos}\n\n"
                        f"Reply: approve {trade_id}  or  reject {trade_id}\n"
                        f"(Expires in 30 min)",
                        to=telegram_to,
                    )
                    signal_found = True
                break  # only one side per game

            if not signal_found:
                pass  # no qualifying signal for this game

    def _execute_approved(self, state, risk, max_pos, cfg, paper_trading, telegram_to):
        """Check pending_trades.json for approved trades and execute them."""
        approved = pending_mgr.get_approved()
        if not approved:
            return

        for t in approved:
            ticker      = t["ticker"]
            side        = t["side"]
            entry_cents = t["entry_cents"]
            trade_id    = t["id"]

            if self.is_already_open(state, ticker):
                pending_mgr.mark_executed(trade_id)
                continue
            if not self.can_open(state, risk, max_pos, cfg):
                self.log.warning("Approved trade skipped — exposure limit reached.")
                break

            contracts = api.usd_to_contracts(max_pos, entry_cents)
            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] Executing approved trade "
                f"{trade_id}: {ticker} {side}@{entry_cents}¢"
            )

            if paper_trading:
                state_mgr.open_position(state, ticker, self.name, side, entry_cents, max_pos)
                pending_mgr.mark_executed(trade_id)
                notifier.send(
                    f"📝 PAPER trade executed [{trade_id}]\n{ticker} {side}@{entry_cents}¢",
                    to=telegram_to,
                )
            else:
                order = api.place_order(ticker, side.lower(), contracts, entry_cents)
                if order is not None:
                    state_mgr.open_position(state, ticker, self.name, side, entry_cents, max_pos)
                    pending_mgr.mark_executed(trade_id)
                    notifier.send(
                        f"✅ LIVE trade placed [{trade_id}]\n"
                        f"{t['title']}\n"
                        f"{side}@{entry_cents}¢ | ${max_pos} | {t['signal']}",
                        to=telegram_to,
                    )
                else:
                    notifier.send(
                        f"❌ Trade {trade_id} FAILED — order rejected by Kalshi\n"
                        f"{ticker} {side}@{entry_cents}¢",
                        to=telegram_to,
                    )
