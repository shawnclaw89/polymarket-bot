"""
Momentum — Buy markets moving significantly on high volume.
"""
from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def scan(self, markets, state, cfg, paper_trading):
        min_vol       = cfg.get("min_volume_24h", 5000)
        min_move      = cfg.get("min_price_move_cents", 8)
        max_pos       = cfg.get("max_position_usd", 10)
        max_entry     = cfg.get("max_entry_cents", 55)
        risk          = state.get("_risk_config", {})

        opps = []
        for m in markets:
            ticker     = m.get("ticker", "")
            title      = m.get("title", "")
            vol        = m.get("volume_24h", 0)
            last_price = m.get("last_price", 0)       # cents
            yes_ask    = m.get("yes_ask", 0)
            no_ask     = m.get("no_ask", 0)

            if vol < min_vol or last_price == 0:
                continue
            if self.is_already_open(state, ticker):
                continue

            # Rough momentum signal: how far from 50¢ center
            move = abs(last_price - 50)
            if move < min_move:
                continue

            side = "YES" if last_price > 50 else "NO"
            entry = yes_ask if side == "YES" else no_ask

            if entry <= 0:
                continue

            # Hard cap: no single trade above max_entry_cents
            if entry > max_entry:
                continue

            opps.append({
                "ticker": ticker, "title": title,
                "side": side, "entry_cents": entry,
                "move": move, "vol": vol,
            })

        opps.sort(key=lambda x: x["move"], reverse=True)

        for opp in opps[:2]:
            if not self.can_open(state, risk, max_pos):
                break

            contracts = api.usd_to_contracts(max_pos, opp["entry_cents"])
            url = self.market_url(opp["ticker"])
            detail = (
                f"{opp['side']} @ {opp['entry_cents']}¢ | "
                f"Move: {opp['move']}¢ from center\n"
                f"24h Vol: {opp['vol']:,} | Contracts: {contracts}"
            )

            self.log.info(f"[{'PAPER' if paper_trading else 'LIVE'}] "
                          f"{opp['title'][:60]} | {opp['side']}@{opp['entry_cents']}¢")
            notifier.opportunity_alert("Momentum 📈", opp["title"][:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        opp["side"], opp["entry_cents"], max_pos)
            else:
                side_key = opp["side"].lower()
                api.place_order(opp["ticker"], side_key, contracts, opp["entry_cents"])
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        opp["side"], opp["entry_cents"], max_pos)
