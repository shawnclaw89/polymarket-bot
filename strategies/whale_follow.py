"""
Whale Follow Strategy
=====================
Kalshi is a centralized exchange — individual user trades are private,
so we can't copy specific wallets like on-chain markets.

Instead, we detect SMART MONEY SIGNALS via order flow:

  1. Volume Surge     — 24h volume spikes far above the market's average
  2. Order Imbalance  — order book heavily skewed to one side (big buyers)
  3. Spread Squeeze   — spread suddenly narrows (confident money moving in)
  4. Large Trade Feed — recent trades show outsized individual fills

These patterns are the same signals whale activity produces on any exchange.
When smart money moves into a position, these fingerprints appear in the data.
"""
from datetime import datetime, timezone
from strategies.base import BaseStrategy
from core import api, notifier, state as state_mgr


class WhaleFollowStrategy(BaseStrategy):
    name = "whale_follow"

    def scan(self, markets, state, cfg, paper_trading):
        min_volume_surge   = cfg.get("min_volume_surge_ratio", 3.0)
        # volume_24h must be X times the open_interest (proxy for avg daily vol)
        min_order_imbal    = cfg.get("min_order_imbalance", 0.70)
        # 70% of order book depth on one side = strong directional bet
        max_spread_cents   = cfg.get("max_spread_cents", 5)
        # Tight spread = confident market (smart money closed the gap)
        min_volume_abs     = cfg.get("min_volume_24h", 2000)
        max_pos            = cfg.get("max_position_usd", 10)
        max_yes            = cfg.get("max_yes_price", 55)
        # Don't follow into already-near-certain markets
        min_yes            = cfg.get("min_yes_price", 20)
        max_close_days     = cfg.get("max_close_days", 7)
        max_entry_cents    = cfg.get("max_entry_cents", 55)
        risk               = state.get("_risk_config", {})

        signals = []

        for m in markets:
            ticker       = m.get("ticker", "")
            title        = m.get("title", "")
            vol_24h      = m.get("volume_24h", 0)
            open_int     = m.get("open_interest", 1) or 1
            yes_ask      = m.get("yes_ask", 0)
            no_ask       = m.get("no_ask", 0)
            yes_bid      = m.get("yes_bid", 0)
            last_price   = m.get("last_price", 50)
            close_time   = m.get("close_time") or m.get("expiration_time", "")

            if vol_24h < min_volume_abs:
                continue
            if self.is_already_open(state, ticker):
                continue
            if yes_ask <= 0 or no_ask <= 0:
                continue
            if not (min_yes <= yes_ask <= max_yes):
                continue

            score = 0
            reasons = []

            # ── Signal 1: Volume Surge ───────────────────────────────────────
            # High 24h volume relative to open interest = unusual activity
            surge_ratio = vol_24h / open_int if open_int > 0 else 0
            if surge_ratio >= min_volume_surge:
                score += 3
                reasons.append(f"📊 Volume surge {surge_ratio:.1f}x ({vol_24h:,} vs OI {open_int:,})")

            # ── Signal 2: Tight Spread (Smart Money Confidence) ──────────────
            # YES: spread = ask - bid. Tight spread = price discovery, confident market
            yes_spread = yes_ask - yes_bid if yes_bid > 0 else 99
            if yes_spread <= max_spread_cents and yes_spread > 0:
                score += 2
                reasons.append(f"📌 Tight spread: {yes_spread}¢")

            # ── Signal 3: Order Book Imbalance ───────────────────────────────
            # If YES ask is much lower than NO ask, buyers are driving price
            total_ask = yes_ask + no_ask
            if total_ask > 0:
                yes_dominance = (100 - yes_ask) / total_ask
                if yes_dominance >= min_order_imbal:
                    score += 2
                    reasons.append(f"⚖️ YES dominance: {yes_dominance:.0%}")
                elif (1 - yes_dominance) >= min_order_imbal:
                    score += 2
                    reasons.append(f"⚖️ NO dominance: {1-yes_dominance:.0%}")

            # ── Signal 4: Price Momentum ─────────────────────────────────────
            # If last trade was significantly off-center, directional pressure exists
            momentum = abs(last_price - 50)
            if momentum >= 15:
                score += 1
                direction = "YES" if last_price > 50 else "NO"
                reasons.append(f"📈 Momentum: {direction} @ {last_price}¢")

            if score < cfg.get("min_signal_score", 3):
                continue

            # Determine which side smart money is on
            side = "YES" if last_price >= 50 else "NO"
            entry = yes_ask if side == "YES" else no_ask

            # Hard cap: never enter above max_entry_cents on either side
            if entry > max_entry_cents:
                continue

            # Time horizon filter
            passes, reason = self.passes_horizon_filter(
                entry, close_time,
                max_entry_cents=max_entry_cents,
                max_close_days=max_close_days,
            )
            if not passes:
                self.log.debug(f"Skipped {ticker}: {reason}")
                continue

            signals.append({
                "ticker": ticker,
                "title": title,
                "side": side,
                "entry_cents": entry,
                "score": score,
                "reasons": reasons,
                "vol_24h": vol_24h,
            })

        # Highest conviction first
        signals.sort(key=lambda x: x["score"], reverse=True)

        for sig in signals[:3]:
            if not self.can_open(state, risk, max_pos, cfg):
                break

            contracts = api.usd_to_contracts(max_pos, sig["entry_cents"])
            url = self.market_url(sig["ticker"])
            detail = (
                f"{sig['side']} @ {sig['entry_cents']}¢ | Score: {sig['score']}/8\n"
                + "\n".join(sig["reasons"])
                + f"\n24h Vol: {sig['vol_24h']:,} | Contracts: {contracts}"
            )

            self.log.info(
                f"[{'PAPER' if paper_trading else 'LIVE'}] Whale signal: "
                f"{sig['title'][:50]} | {sig['side']}@{sig['entry_cents']}¢ | score={sig['score']}"
            )
            notifier.opportunity_alert("🐋 Whale Signal", sig["title"][:80], detail, url)

            if paper_trading:
                state_mgr.open_position(state, sig["ticker"], self.name,
                                        sig["side"], sig["entry_cents"], max_pos)
            else:
                order = api.place_order(sig["ticker"], sig["side"].lower(),
                                        contracts, sig["entry_cents"])
                if order is not None:
                    state_mgr.open_position(state, sig["ticker"], self.name,
                                            sig["side"], sig["entry_cents"], max_pos)
