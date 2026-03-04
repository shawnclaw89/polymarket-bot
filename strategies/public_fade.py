"""
Public Fade Strategy — requires user approval before any trade fires
====================================================================
Flow:
  1. Fetch Kalshi game-winner markets (both "X vs Y" and "X at Y" formats)
  2. Group by close date, fetch Action Network data per date
  3. Signal = public % >> money % divergence on same team
  4. Validate sport via ticker prefix (KXNBAGAME/KXNHLGAME/KXMLBSTGAME)
  5. Queue signal → Telegram alert → wait for user approval → execute
"""
import uuid
from datetime import datetime, timezone, timedelta
import requests

from strategies.base import BaseStrategy
from strategies.polymarket_tail import TEAM_ALIASES
from core import api, notifier, state as state_mgr, pending as pending_mgr
from core.health import cached

# ── Ticker-prefix sport detection (most reliable) ────────────────────────────
TICKER_SPORT_MAP = {
    "KXNBAGAME":    "NBA",
    "KXNBA1HWINNER":"NBA",
    "KXNBA2HWINNER":"NBA",
    "KXNHLGAME":    "NHL",
    "KXMLBSTGAME":  "MLB",
    "KXMLBWBCGAME": "MLB",
}

def sport_from_ticker(ticker: str) -> str | None:
    for prefix, sport in TICKER_SPORT_MAP.items():
        if ticker.startswith(prefix):
            return sport
    return None

# ── Team abbreviation → full name (from Kalshi ticker suffix) ────────────────
NBA_ABBREV = {
    "ATL":"atlanta hawks","BOS":"boston celtics","BKN":"brooklyn nets",
    "CHA":"charlotte hornets","CHI":"chicago bulls","CLE":"cleveland cavaliers",
    "DAL":"dallas mavericks","DEN":"denver nuggets","DET":"detroit pistons",
    "GSW":"golden state warriors","HOU":"houston rockets","IND":"indiana pacers",
    "LAC":"los angeles clippers","LAL":"los angeles lakers","MEM":"memphis grizzlies",
    "MIA":"miami heat","MIL":"milwaukee bucks","MIN":"minnesota timberwolves",
    "NOP":"new orleans pelicans","NYK":"new york knicks","OKC":"oklahoma city thunder",
    "ORL":"orlando magic","PHI":"philadelphia 76ers","PHX":"phoenix suns",
    "POR":"portland trail blazers","SAC":"sacramento kings","SAS":"san antonio spurs",
    "TOR":"toronto raptors","UTA":"utah jazz","WAS":"washington wizards",
}
NHL_ABBREV = {
    "ANA":"anaheim ducks","BOS":"boston bruins","BUF":"buffalo sabres",
    "CAR":"carolina hurricanes","CBJ":"columbus blue jackets","CGY":"calgary flames",
    "CHI":"chicago blackhawks","COL":"colorado avalanche","DAL":"dallas stars",
    "DET":"detroit red wings","EDM":"edmonton oilers","FLA":"florida panthers",
    "LA":"los angeles kings","MIN":"minnesota wild","MTL":"montreal canadiens",
    "NSH":"nashville predators","NJ":"new jersey devils","NYI":"new york islanders",
    "NYR":"new york rangers","OTT":"ottawa senators","PHI":"philadelphia flyers",
    "PIT":"pittsburgh penguins","SEA":"seattle kraken","SJ":"san jose sharks",
    "STL":"st. louis blues","TB":"tampa bay lightning","TOR":"toronto maple leafs",
    "UTA":"utah mammoth","VAN":"vancouver canucks","VGK":"vegas golden knights",
    "WSH":"washington capitals","WPG":"winnipeg jets",
}
ALL_ABBREV = {**NBA_ABBREV, **NHL_ABBREV}

# Extra Kalshi-specific title fragments → full team name (for "at" format)
KALSHI_TEAM_NAMES = {
    "los angeles l":   "los angeles lakers",
    "los angeles c":   "los angeles clippers",
    "new york i":      "new york islanders",
    "new york r":      "new york rangers",
    "new york k":      "new york knicks",
    "golden state":    "golden state warriors",
    "oklahoma city":   "oklahoma city thunder",
    "san antonio":     "san antonio spurs",
    "new orleans":     "new orleans pelicans",
    "utah":            "utah jazz",           # NBA
    "indiana":         "indiana pacers",
    "portland":        "portland trail blazers",
    "sacramento":      "sacramento kings",
    "memphis":         "memphis grizzlies",
    "minnesota":       "minnesota timberwolves",
    "toronto":         "toronto raptors",
    "denver":          "denver nuggets",
    "dallas":          "dallas mavericks",
    "miami":           "miami heat",
    "charlotte":       "charlotte hornets",
    "chicago":         "chicago bulls",
    "brooklyn":        "brooklyn nets",
    "phoenix":         "phoenix suns",
    "detroit":         "detroit pistons",
    "cleveland":       "cleveland cavaliers",
    "milwaukee":       "milwaukee bucks",
    "atlanta":         "atlanta hawks",
    "boston":          "boston celtics",
    # NHL
    "winnipeg":        "winnipeg jets",
    "edmonton":        "edmonton oilers",
    "ottawa":          "ottawa senators",
    "calgary":         "calgary flames",
    "vancouver":       "vancouver canucks",
    "montreal":        "montreal canadiens",
    "nashville":       "nashville predators",
    "columbus":        "columbus blue jackets",
    "pittsburgh":      "pittsburgh penguins",
    "buffalo":         "buffalo sabres",
    "anaheim":         "anaheim ducks",
    "seattle":         "seattle kraken",
    "colorado":        "colorado avalanche",
    "carolina":        "carolina hurricanes",
    "florida":         "florida panthers",
    "tampa bay":       "tampa bay lightning",
    "vegas":           "vegas golden knights",
    "st. louis":       "st. louis blues",
    "washington":      "washington capitals",
    "new jersey":      "new jersey devils",
    "philadelphia":    "philadelphia flyers",  # NHL
}

def resolve_yes_team(ticker: str) -> str | None:
    suffix = ticker.split("-")[-1].upper()
    return ALL_ABBREV.get(suffix)


# ── Action Network ────────────────────────────────────────────────────────────
AN_BASE = "https://api.actionnetwork.com/web/v1/scoreboard"
AN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept":     "application/json",
}
CONSENSUS_BOOK_ID = 15
SPORTS = ["nba", "mlb", "nfl", "nhl", "ncaab"]

_session = requests.Session()
_session.headers.update(AN_HEADERS)


def get_an_leans_for_date(date_str: str, sport: str) -> list:
    """Fetch AN games for a specific date (YYYYMMDD) and sport."""
    cache_key = f"an_{sport}_{date_str}"
    def fetch():
        try:
            r = _session.get(
                f"{AN_BASE}/{sport}",
                params={"date": date_str},
                timeout=10,
            )
            r.raise_for_status()
            leans = []
            for game in r.json().get("games", []):
                lean = _extract_lean(game, sport)
                if lean:
                    leans.append(lean)
            return leans
        except Exception:
            return []
    return cached(cache_key, ttl=300, fetch_fn=fetch) or []


def _extract_lean(game: dict, sport: str) -> dict | None:
    teams   = {t["id"]: t for t in game.get("teams", [])}
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
    # Check Kalshi-specific name first
    mapped = KALSHI_TEAM_NAMES.get(frag)
    if mapped and mapped in full:
        score += 5
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
    frag = frag.lower().strip()
    # Check Kalshi-specific name mapping first
    mapped = KALSHI_TEAM_NAMES.get(frag)
    if mapped and mapped in signals:
        return mapped, signals[mapped]
    best_name, best_sig, best_score = None, None, 0
    for name, sig in signals.items():
        score = _team_score(frag, name)
        if score > best_score:
            best_score, best_name, best_sig = score, name, sig
    return (best_name, best_sig) if best_score >= 3 else (None, None)


def parse_teams(title: str, ticker: str) -> tuple[str, str]:
    """
    Parse Kalshi title to get (frag_yes, frag_no).
    Returns frags for the YES-side team and NO-side team.
    """
    tl = title.lower()
    if " vs " in tl:
        parts  = tl.split(" vs ", 1)
        frag_a = parts[0].strip()
        frag_b = parts[1].replace("winner?","").replace("winner","").strip()
    elif " at " in tl:
        parts     = tl.split(" at ", 1)
        frag_away = parts[0].strip()
        frag_home = parts[1].replace("winner?","").replace("winner","").strip()
        frag_a, frag_b = frag_away, frag_home
    else:
        return "", ""

    # Resolve YES team from ticker suffix
    yes_team = resolve_yes_team(ticker)
    if yes_team:
        sa = _team_score(frag_a, yes_team)
        sb = _team_score(frag_b, yes_team)
        if sa >= sb:
            return frag_a, frag_b   # frag_a = YES
        else:
            return frag_b, frag_a   # frag_b = YES
    # Fallback: first team = YES
    return frag_a, frag_b


_MONTH_MAP = {
    "JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
    "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12",
}

def game_date_from_ticker(ticker: str) -> str | None:
    """
    Extract YYYYMMDD game date from Kalshi ticker.
    e.g. KXNBAGAME-26MAR05TORMIN-MIN  →  20260305
         KXNHLGAME-26MAR06CAREDM-CAR  →  20260306
    """
    import re
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker.upper())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    mm = _MONTH_MAP.get(mon)
    if not mm:
        return None
    return f"20{yy}{mm}{dd}"

def kalshi_close_date_str(close_time: str) -> str:
    """Fallback: return YYYYMMDD from close time (UTC-5)."""
    try:
        dt_utc = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        dt_et  = dt_utc - timedelta(hours=5)
        return dt_et.strftime("%Y%m%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%d")


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

        # ── Step 0: Execute approved trades ──────────────────────────────────
        self._execute_approved(state, risk, max_pos, cfg, paper_trading, telegram_to)

        # ── Step 1: Filter Kalshi game markets ────────────────────────────────
        seen_games = set()
        game_markets = []
        for m in markets:
            if m.get("_category", "").lower() != "sports":
                continue
            title = m.get("title", "")
            tl    = title.lower()
            if " vs " not in tl and " at " not in tl:
                continue
            if "winner" not in tl:
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
            game_id = ticker.rsplit("-", 1)[0]
            if game_id in seen_games:
                continue
            if self.is_already_open(state, ticker):
                continue
            if pending_mgr.is_game_already_queued(game_id):
                continue
            seen_games.add(game_id)
            m["_close_time"] = ct
            game_markets.append(m)

        if not game_markets:
            self.log.info("No new Kalshi game markets to check.")
            return

        self.log.info(f"Checking {len(game_markets)} unique Kalshi game markets.")

        # ── Step 2: Group by (sport, date) and fetch AN data once per group ──
        an_signal_cache: dict[str, dict] = {}  # cache key → team signals dict

        def get_signals(sport_key: str, date_str: str) -> dict:
            key = f"{sport_key}_{date_str}"
            if key not in an_signal_cache:
                leans = get_an_leans_for_date(date_str, sport_key.lower())
                an_signal_cache[key] = build_team_signals(leans)
            return an_signal_cache[key]

        # ── Step 3: Check each market ─────────────────────────────────────────
        for km in game_markets:
            ticker  = km.get("ticker", "")
            title   = km.get("title", "")
            yes_ask = km.get("yes_ask", 0)
            no_ask  = km.get("no_ask", 0)
            ct      = km.get("_close_time", "")

            sport = sport_from_ticker(ticker)
            if not sport:
                continue  # only trade markets we can identify

            # Use game date from ticker (e.g. 26MAR05 → 20260305)
            # NOT the settlement/close date which is weeks later
            date_str = game_date_from_ticker(ticker) or kalshi_close_date_str(ct)
            signals  = get_signals(sport, date_str)
            if not signals:
                continue

            frag_yes, frag_no = parse_teams(title, ticker)
            if not frag_yes:
                continue

            yes_name, yes_sig = find_team_signal(frag_yes, signals)
            no_name,  no_sig  = find_team_signal(frag_no,  signals)

            if not yes_sig and not no_sig:
                continue

            # Validate sport matches AN signal
            for fade_side, pub_name, sig, entry_cents in [
                ("NO",  yes_name, yes_sig, no_ask),
                ("YES", no_name,  no_sig,  yes_ask),
            ]:
                if not sig or not entry_cents:
                    continue
                if sig["sport"] != sport:
                    self.log.debug(
                        f"Sport mismatch: Kalshi={sport} AN={sig['sport']} — "
                        f"skip {title[:50]}"
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
                    f"[{sport}] Public {pub_pct}% bets / {money_pct}% money "
                    f"on {pub_name} (+{divergence:.0f}pt)"
                )

                queued = pending_mgr.add(trade_id, {
                    "ticker":      ticker,
                    "kalshi_side": fade_side,
                    "entry_cents": entry_cents,
                    "size_usd":    max_pos,
                    "title":       title,
                    "signal":      signal_summary,
                    "sport":       sport,
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
                break  # one side per game

    def _execute_approved(self, state, risk, max_pos, cfg, paper_trading, telegram_to):
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
                notifier.send(f"📝 PAPER [{trade_id}] {ticker} {side}@{entry_cents}¢", to=telegram_to)
            else:
                order = api.place_order(ticker, side.lower(), contracts, entry_cents)
                if order is not None:
                    state_mgr.open_position(state, ticker, self.name, side, entry_cents, max_pos)
                    pending_mgr.mark_executed(trade_id)
                    notifier.send(
                        f"✅ LIVE trade placed [{trade_id}]\n"
                        f"{t['title']}\n{side}@{entry_cents}¢ | ${max_pos}\n{t['signal']}",
                        to=telegram_to,
                    )
                else:
                    notifier.send(
                        f"❌ Trade {trade_id} FAILED — order rejected\n{ticker} {side}@{entry_cents}¢",
                        to=telegram_to,
                    )
