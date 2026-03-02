"""
Intramarket Arb — YES ask + NO ask should = 100¢.
When sum < 100¢, buying both locks in guaranteed profit.
Example: YES@44¢ + NO@54¢ = 98¢ → profit = 2¢ per pair guaranteed.
"""
from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr


class IntramarketArbStrategy(BaseStrategy):
    name = "intramarket_arb"

    def scan(self, markets, state, cfg, paper_trading):
        min_gap       = cfg.get("min_gap_cents", 2)
        max_pos       = cfg.get("max_position_usd", 10)
        min_liq       = cfg.get("min_liquidity", 200)
        max_leg_cents = cfg.get("max_entry_cents", 55)   # cap each leg individually
        risk          = state.get("_risk_config", {})

        opps = []
        for m in markets:
            ticker  = m.get("ticker", "")
            title   = m.get("title", "")
            yes_ask = m.get("yes_ask", 0)
            no_ask  = m.get("no_ask", 0)
            liq     = m.get("liquidity", 0)

            if yes_ask <= 0 or no_ask <= 0 or liq < min_liq:
                continue

            # Skip arbs where either leg exceeds the per-trade price cap
            if yes_ask > max_leg_cents or no_ask > max_leg_cents:
                continue

            total = yes_ask + no_ask
            gap   = 100 - total

            if gap < min_gap:
                continue
            if self.is_already_open(state, ticker):
                continue

            profit_pct = (gap / total) * 100

            opps.append({
                "ticker": ticker, "title": title,
                "yes_ask": yes_ask, "no_ask": no_ask,
                "gap": gap, "profit_pct": round(profit_pct, 2),
                "liquidity": liq,
            })

        opps.sort(key=lambda x: x["gap"], reverse=True)

        for opp in opps[:3]:
            if not self.can_open(state, risk, max_pos):
                break

            # Buy both sides
            yes_contracts = api.usd_to_contracts(max_pos / 2, opp["yes_ask"])
            no_contracts  = api.usd_to_contracts(max_pos / 2, opp["no_ask"])
            url = self.market_url(opp["ticker"])
            detail = (
                f"YES@{opp['yes_ask']}¢ + NO@{opp['no_ask']}¢ = {opp['yes_ask']+opp['no_ask']}¢\n"
                f"Gap: {opp['gap']}¢ | Profit: +{opp['profit_pct']:.2f}% guaranteed\n"
                f"Liquidity: {opp['liquidity']:,}"
            )

            self.log.info(f"[{'PAPER' if paper_trading else 'LIVE'}] "
                          f"{opp['title'][:60]} | gap={opp['gap']}¢ | +{opp['profit_pct']:.2f}%")
            notifier.opportunity_alert("Intramarket Arb ⚖️", opp["title"][:80], detail, url)

            if paper_trading:
                avg_price = (opp["yes_ask"] + opp["no_ask"]) / 2
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        "BOTH", avg_price, max_pos)
            else:
                api.place_order(opp["ticker"], "yes", yes_contracts, opp["yes_ask"])
                api.place_order(opp["ticker"], "no",  no_contracts,  opp["no_ask"])
                avg_price = (opp["yes_ask"] + opp["no_ask"]) / 2
                state_mgr.open_position(state, opp["ticker"], self.name,
                                        "BOTH", avg_price, max_pos)
