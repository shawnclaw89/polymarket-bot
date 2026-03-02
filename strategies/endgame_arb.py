"""
Endgame Arb — Buy near-certain Kalshi markets that are ending soon.

Filters:
  - YES price < 96¢ (room for return, not already at ceiling)
  - Market closes within `max_hours_to_close` hours (ending soon)
  - High enough volume to ensure liquidity

Example: YES @ 92¢, closes in 4h → resolves 100¢ = +8.7% in hours
"""
from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr


class EndgameArbStrategy(BaseStrategy):
    name = "endgame_arb"

    def scan(self, markets, state, cfg, paper_trading):
        min_yes         = cfg.get("min_yes_price", 80)       # cents — lower floor, more room
        max_yes         = cfg.get("max_yes_price", 95)       # cents — must be < 96
        min_vol         = cfg.get("min_volume_24h", 500)
        max_pos         = cfg.get("max_position_usd", 20)
        min_ret         = cfg.get("min_return_pct", 0.5) / 100
        max_hours       = cfg.get("max_hours_to_close", 24)  # only "ending soon" markets
        risk            = state.get("_risk_config", {})

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=max_hours)

        opps = []
        for m in markets:
            ticker     = m.get("ticker", "")
            title      = m.get("title", "")
            yes_ask    = m.get("yes_ask", 0)
            vol        = m.get("volume_24h", 0)
            close_time = m.get("close_time") or m.get("expiration_time")

            # Must have a close time and be ending soon
            if not close_time:
                continue
            try:
                closes_at = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except Exception:
                continue

            # Skip if not closing within our window, or already closed
            if closes_at <= now or closes_at > cutoff:
                continue

            hours_left = (closes_at - now).total_seconds() / 3600

            # Price must be < 96¢ and above floor
            if not (min_yes <= yes_ask <= max_yes):
                continue
            if vol < min_vol:
                continue
            if self.is_already_open(state, ticker):
                continue

            expected_ret = (100 - yes_ask) / yes_ask
            if expected_ret < min_ret:
                continue

            opps.append({
                "ticker": ticker,
                "title": title,
                "yes_ask": yes_ask,
                "ret_pct": round(expected_ret * 100, 2),
                "vol": vol,
                "hours_left": round(hours_left, 1),
                "closes_at": closes_at,
            })

        # Sort: closest to closing first (highest urgency)
        opps.sort(key=lambda x: x["hours_left"])

        for opp in opps[:3]:
            if not self.can_open(state, risk, max_pos):
                break

            contracts = api.usd_to_contracts(max_pos, opp["yes_ask"])
            url = self.market_url(opp["ticker"])
            detail = (
                f"YES @ {opp['yes_ask']}¢ → resolves 100¢\n"
                f"Return: +{opp['ret_pct']:.2f}% | Closes in: {opp['hours_left']}h\n"
                f"Contracts: {contracts} | 24h Vol: {opp['vol']:,}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] "
                f"{opp['title'][:60]} | YES@{opp['yes_ask']}¢ | "
                f"+{opp['ret_pct']:.2f}% | closes in {opp['hours_left']}h"
            )
            notifier.opportunity_alert("Endgame Arb 🎯", opp["title"][:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        "YES", opp["yes_ask"], max_pos)
            else:
                api.place_order(opp["ticker"], "yes", contracts, opp["yes_ask"])
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        "YES", opp["yes_ask"], max_pos)
