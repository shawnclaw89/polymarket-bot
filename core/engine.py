"""Main bot engine — loads config, strategies, runs the loop."""
import importlib, logging, os, sys, time
import yaml
from core import api, notifier, state as state_mgr, auth, health

log = logging.getLogger("engine")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

STRATEGY_MODULES = {
    "endgame_arb":     "strategies.endgame_arb:EndgameArbStrategy",
    "intramarket_arb": "strategies.intramarket_arb:IntramarketArbStrategy",
    "momentum":        "strategies.momentum:MomentumStrategy",
    "whale_follow":    "strategies.whale_follow:WhaleFollowStrategy",
    "polymarket_tail": "strategies.polymarket_tail:PolymarketTailStrategy",
    "agent_signal":    "strategies.agent_signal:AgentSignalStrategy",
    "public_fade":     "strategies.public_fade:PublicFadeStrategy",
    # Add new strategies here
}


def load_config():
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def load_strategies(config):
    loaded = []
    for name, path in STRATEGY_MODULES.items():
        cfg = config.get("strategies", {}).get(name, {})
        if not cfg.get("enabled", False):
            log.info(f"Strategy '{name}' disabled.")
            continue
        try:
            mod_name, cls_name = path.split(":")
            cls = getattr(importlib.import_module(mod_name), cls_name)
            loaded.append(cls())
            log.info(f"✅ Loaded: {name}")
        except Exception as e:
            log.error(f"Failed to load '{name}': {e}")
    return loaded


ARB_STRATEGIES = {"endgame_arb", "intramarket_arb"}
SIGNAL_STRATEGIES = {"whale_follow", "polymarket_tail", "agent_signal", "momentum", "public_fade"}


def run_once(strategies, config, arb_only=False):
    paper    = config.get("paper_trading", True)
    risk     = config.get("risk", {})
    max_alerts = config.get("max_alerts_per_scan", 3)
    telegram_to = config.get("telegram_to", "7591705971")
    health_interval = config.get("health_check_interval", 600)

    # ── Health check (throttled, alerts only on change) ──────────────────────
    health.run_checks(to=telegram_to, check_interval=health_interval)

    # Skip if Kalshi is down
    if not health.is_kalshi_ok():
        log.warning("Kalshi API unavailable — skipping scan.")
        return

    if arb_only:
        log.debug(f"--- Arb tick [{'PAPER' if paper else 'LIVE'}] ---")
    else:
        log.info(f"--- Full tick [{'PAPER' if paper else 'LIVE'}] ---")

    # ── Balance guard (live only, full ticks only to avoid hammering API) ─────
    if not paper and not arb_only:
        balance = api.get_balance()
        if balance is None:
            log.error("Could not fetch account balance — skipping tick.")
            return
        if balance <= 0:
            msg = (
                "🛑 Kalshi bot STOPPED\n"
                "Account balance is $0.00 — no funds available to trade.\n"
                "Add funds manually at kalshi.com, then restart the bot."
            )
            log.error(msg)
            notifier.send(msg, to=telegram_to)
            sys.exit(0)
        log.info(f"💰 Account balance: ${balance:.2f}")
        state = state_mgr.load()
        state["live_balance"] = balance

        # ── Auto-clear settled positions ─────────────────────────────────────
        # Sync state vs real Kalshi positions — only clear if API call succeeds
        try:
            live_positions = api.get_positions()
            if live_positions is not None:  # None = API error; [] = genuinely empty
                live_tickers = set()
                for p in live_positions:
                    t = getattr(p, "ticker", None) or (p.get("ticker", "") if isinstance(p, dict) else "")
                    if t:
                        live_tickers.add(t)
                state_tickers = set(state.get("positions", {}).keys())
                stale = state_tickers - live_tickers
                if stale and len(live_tickers) > 0:
                    # Only auto-clear if Kalshi returned SOME positions
                    # (if live_tickers is empty but we have state, it might be an API issue)
                    for t in stale:
                        log.info(f"✅ Settled — removing from state: {t}")
                        state["positions"].pop(t, None)
                    notifier.send(
                        f"✅ {len(stale)} position(s) settled & cleared from state\n"
                        + "\n".join(f"  • {t}" for t in stale),
                        to=telegram_to,
                    )
                elif stale and len(live_tickers) == 0:
                    log.info(
                        f"Kalshi returned 0 live positions but state has {len(stale)} — "
                        "skipping auto-clear (possible API gap or all positions settling)"
                    )
        except Exception as e:
            log.warning(f"Position sync failed (non-critical): {e}")

        state_mgr.save(state)

    markets, _ = api.get_markets(max_pages=20)
    if not markets:
        log.error("No markets returned.")
        return

    if not arb_only:
        log.info(f"Fetched {len(markets)} markets.")

    state = state_mgr.load()
    state["_risk_config"] = risk

    # ── Begin alert batch ────────────────────────────────────────────────────
    notifier.begin_batch()

    for strategy in strategies:
        # Fast-tick mode: only run arb strategies
        if arb_only and strategy.name not in ARB_STRATEGIES:
            continue

        # Skip PolymarketScan-dependent strategies if API is down
        if strategy.name in ("polymarket_tail", "agent_signal", "whale_follow"):
            if not health.is_polymarketscan_ok():
                log.warning(f"PolymarketScan down — skipping {strategy.name}")
                continue

        try:
            cfg = config.get("strategies", {}).get(strategy.name, {})
            strategy.scan(markets, state, cfg, paper)
        except Exception as e:
            log.exception(f"Strategy '{strategy.name}' crashed: {e}")

    # ── Flush batch — ONE message max N alerts ───────────────────────────────
    notifier.flush_batch(max_alerts=max_alerts, to=telegram_to)

    state.pop("_risk_config", None)
    state_mgr.save(state)

    if not arb_only:
        log.info(
            f"Positions: {state_mgr.position_count(state)} | "
            f"Exposure: ${state_mgr.total_exposure(state):.2f} | "
            f"Daily P&L: ${state.get('daily_pnl', 0):.2f}"
        )


def run(once=False):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")],
    )

    config       = load_config()
    paper        = config.get("paper_trading", True)
    interval     = config.get("scan_interval", 300)       # full scan: signal strategies
    arb_interval = config.get("scan_interval_arb", 30)    # fast scan: arb only

    # Authenticate
    authenticated = auth.init(
        api_key_id=config.get("api_key_id", ""),
        private_key_path=config.get("private_key_path", "kalshi_private_key.pem"),
        host=config.get("kalshi_api_host", "https://api.elections.kalshi.com/trade-api/v2"),
    )

    if not paper and not authenticated:
        log.error("Live trading requires valid API credentials.")
        return

    strategies = load_strategies(config)
    if not strategies:
        log.warning("No strategies enabled.")
        return

    # Initial health check
    log.info("Running startup health check...")
    health.run_checks(to=config.get("telegram_to", "7591705971"), check_interval=0)

    mode = "📝 PAPER" if paper else "🔴 LIVE"
    log.info(
        f"Bot starting — {mode} | {len(strategies)} strategies | "
        f"arb: {arb_interval}s | full: {interval}s"
    )
    notifier.send(
        f"🤖 Kalshi Bot started\n"
        f"Mode: {mode}\n"
        f"Strategies: {', '.join(s.name for s in strategies)}\n"
        f"Arb scan: every {arb_interval}s | Full scan: every {interval}s\n"
        f"Kalshi: {'✅' if health.is_kalshi_ok() else '❌'} | "
        f"PolymarketScan: {'✅' if health.is_polymarketscan_ok() else '❌'}",
        to=config.get("telegram_to", "7591705971"),
    )

    if once:
        run_once(strategies, config)
        return

    last_full_scan = 0.0
    while True:
        now = time.time()
        # Full scan (all strategies) when slow interval has elapsed
        if now - last_full_scan >= interval:
            run_once(strategies, config, arb_only=False)
            last_full_scan = time.time()
        else:
            # Fast tick — arb strategies only
            run_once(strategies, config, arb_only=True)

        log.debug(f"Sleeping {arb_interval}s (arb interval)...")
        time.sleep(arb_interval)
