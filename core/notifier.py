"""
Notifier — batched Telegram alerts via OpenClaw CLI.

Batching model:
  - engine calls begin_batch() at scan start
  - strategies call queue_alert() / queue_info() — nothing sends yet
  - engine calls flush_batch(max_alerts=3) at scan end
  - ONE message sent per scan containing top N alerts
  - Suppresses noise (no-match info) from reaching Telegram

Direct send() still available for startup/error messages.
"""
import subprocess
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Batch queue (populated during scan, flushed at end) ─────────────────────
_batch: list[dict] = []
_in_batch = False


def begin_batch():
    global _batch, _in_batch
    _batch = []
    _in_batch = True


def flush_batch(max_alerts: int = 3, to: str = "7591705971"):
    global _batch, _in_batch
    _in_batch = False

    if not _batch:
        return

    # Sort: actionable trades first, then opportunities, then info
    priority = {"trade": 0, "opportunity": 1, "info": 2}
    _batch.sort(key=lambda x: priority.get(x["kind"], 9))

    # Take top N
    to_send = _batch[:max_alerts]
    skipped = len(_batch) - len(to_send)

    lines = []
    for item in to_send:
        lines.append(item["text"])
        lines.append("")  # spacer

    if skipped > 0:
        lines.append(f"_+{skipped} more signals filtered — see logs_")

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines.append(f"_Scan: {ts}_")

    send("\n".join(lines).strip(), to=to)
    _batch = []


def _queue(kind: str, text: str):
    """Add to batch queue, or send immediately if not in batch mode."""
    if _in_batch:
        _batch.append({"kind": kind, "text": text})
    else:
        send(text)


# ── Public API ───────────────────────────────────────────────────────────────

def send(message: str, to: str = "7591705971"):
    """Send immediately (bypasses batch — use for startup/errors/health)."""
    try:
        r = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "telegram",
             "--target", to,
             "--message", message],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            log.info("Alert sent.")
        else:
            log.error(f"Send failed: {r.stderr.strip()}")
    except FileNotFoundError:
        print(message)
    except Exception as e:
        log.error(f"Notifier error: {e}")


def opportunity_alert(strategy: str, market: str, detail: str, url: str = ""):
    """Queue an actionable opportunity alert."""
    lines = [f"💡 *{strategy}*", market, detail]
    if url:
        lines.append(f"🔗 {url}")
    _queue("opportunity", "\n".join(lines))


def trade_alert(strategy: str, action: str, market: str, price_cents: int,
                size_usd: float, paper: bool = True, url: str = ""):
    """Queue a trade execution alert (highest priority)."""
    mode = "📝 PAPER" if paper else "🔴 LIVE"
    lines = [
        f"{mode} | *{strategy}*",
        f"{'🟢 BUY' if action == 'buy' else '🔴 SELL'} {market}",
        f"Price: {price_cents}¢ | Size: ${size_usd:.2f}",
    ]
    if url:
        lines.append(f"🔗 {url}")
    _queue("trade", "\n".join(lines))


def info(text: str):
    """Low-priority info — only sent if batch has room after real signals."""
    _queue("info", text)
