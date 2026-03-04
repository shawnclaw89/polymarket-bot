"""
Pending trade approval queue.
Bot writes signals here; they don't execute until the user approves via Telegram.
"""
import json, os, logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("pending")
PENDING_FILE = os.path.join(os.path.dirname(__file__), "..", "pending_trades.json")
EXPIRY_MINUTES = 30   # auto-expire unacted signals after 30 min


def _load() -> dict:
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"trades": []}


def _save(data: dict):
    with open(PENDING_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add(trade_id: str, trade: dict):
    """Queue a trade for user approval."""
    data = _load()
    # Don't double-queue same ticker
    for t in data["trades"]:
        if t["ticker"] == trade["ticker"] and t["status"] == "pending":
            return False
    expires = (datetime.now(timezone.utc) + timedelta(minutes=EXPIRY_MINUTES)).isoformat()
    data["trades"].append({
        "id":          trade_id,
        "ticker":      trade["ticker"],
        "side":        trade["kalshi_side"],
        "entry_cents": trade["entry_cents"],
        "size_usd":    trade["size_usd"],
        "title":       trade["title"],
        "signal":      trade["signal"],
        "sport":       trade["sport"],
        "status":      "pending",   # pending | approved | rejected | expired | executed
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "expires_at":  expires,
    })
    _save(data)
    log.info(f"Queued trade {trade_id}: {trade['ticker']} {trade['kalshi_side']}@{trade['entry_cents']}¢")
    return True


def approve(trade_id: str) -> bool:
    """Mark a trade approved. Returns True if found."""
    data = _load()
    for t in data["trades"]:
        if t["id"] == trade_id and t["status"] == "pending":
            t["status"] = "approved"
            t["approved_at"] = datetime.now(timezone.utc).isoformat()
            _save(data)
            log.info(f"Trade {trade_id} approved.")
            return True
    return False


def reject(trade_id: str) -> bool:
    """Mark a trade rejected. Returns True if found."""
    data = _load()
    for t in data["trades"]:
        if t["id"] == trade_id and t["status"] == "pending":
            t["status"] = "rejected"
            _save(data)
            log.info(f"Trade {trade_id} rejected.")
            return True
    return False


def get_approved() -> list:
    """Return all approved (not yet executed) trades, expiring stale ones."""
    data = _load()
    now = datetime.now(timezone.utc)
    changed = False
    approved = []
    for t in data["trades"]:
        if t["status"] in ("executed", "rejected", "expired"):
            continue
        expires = datetime.fromisoformat(t["expires_at"])
        if now > expires and t["status"] == "pending":
            t["status"] = "expired"
            changed = True
            log.info(f"Trade {t['id']} expired.")
            continue
        if t["status"] == "approved":
            approved.append(t)
    if changed:
        _save(data)
    return approved


def mark_executed(trade_id: str):
    data = _load()
    for t in data["trades"]:
        if t["id"] == trade_id:
            t["status"] = "executed"
            t["executed_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)


def get_pending() -> list:
    """Return all currently pending (unanswered) trades."""
    data = _load()
    now = datetime.now(timezone.utc)
    changed = False
    pending = []
    for t in data["trades"]:
        if t["status"] != "pending":
            continue
        expires = datetime.fromisoformat(t["expires_at"])
        if now > expires:
            t["status"] = "expired"
            changed = True
            continue
        pending.append(t)
    if changed:
        _save(data)
    return pending


def is_game_already_queued(game_id: str) -> bool:
    """Check if any trade for this game base ID is already pending/approved."""
    data = _load()
    for t in data["trades"]:
        if t["status"] in ("pending", "approved") and game_id in t["ticker"]:
            return True
    return False
