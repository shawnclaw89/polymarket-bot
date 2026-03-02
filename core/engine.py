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


def run_once(strategies, config):
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

    log.info(f"--- Tick [{'PAPER' if paper else 'LIVE'}] ---")

    markets, _ = api.get_markets(limit=200)
    if not markets:
        log.error("No markets returned.")
        return

    log.info(f"Fetched {len(markets)} markets.")

    state = state_mgr.load()
    state["_risk_config"] = risk

    # ── Begin alert batch ────────────────────────────────────────────────────
    notifier.begin_batch()

    for strategy in strategies:
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

    config   = load_config()
    paper    = config.get("paper_trading", True)
    interval = config.get("scan_interval", 300)

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
    log.info(f"Bot starting — {mode} | {len(strategies)} strategies | interval: {interval}s")
    notifier.send(
        f"🤖 Kalshi Bot started\n"
        f"Mode: {mode}\n"
        f"Strategies: {', '.join(s.name for s in strategies)}\n"
        f"Kalshi: {'✅' if health.is_kalshi_ok() else '❌'} | "
        f"PolymarketScan: {'✅' if health.is_polymarketscan_ok() else '❌'}",
        to=config.get("telegram_to", "7591705971"),
    )

    if once:
        run_once(strategies, config)
        return

    while True:
        run_once(strategies, config)
        log.info(f"Sleeping {interval}s...")
        time.sleep(interval)
