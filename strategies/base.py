"""Base class for all Kalshi strategies."""
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
import logging
from core import state as state_mgr

class BaseStrategy(ABC):
    name: str = "unnamed"

    def __init__(self):
        self.log = logging.getLogger(f"strategy.{self.name}")

    @abstractmethod
    def scan(self, markets: list, state: dict, cfg: dict, paper_trading: bool):
        """
        Called each tick.
        markets: list of Kalshi market dicts (prices in CENTS 0-100)
        state:   shared mutable state
        cfg:     this strategy's config block
        paper_trading: True = simulate only
        """
        ...

    def market_url(self, ticker: str) -> str:
        from core.api import market_url
        return market_url(ticker)

    def is_already_open(self, state, ticker):
        return ticker in state.get("positions", {})

    def can_open(self, state, risk, size_usd):
        if state_mgr.position_count(state) >= risk.get("max_open_positions", 10):
            self.log.debug("Max positions reached.")
            return False
        if state_mgr.total_exposure(state) + size_usd > risk.get("max_total_exposure_usd", 200):
            self.log.debug("Max exposure reached.")
            return False
        if state.get("daily_pnl", 0) <= -risk.get("max_daily_loss_usd", 50):
            self.log.warning("Daily loss limit hit.")
            return False
        return True

    def passes_horizon_filter(self, entry_cents: int, close_time_str: str,
                               max_entry_cents: int = 55,
                               max_close_days: int = 7,
                               long_horizon_days: int = 7,
                               long_horizon_max_cents: int = 15) -> tuple[bool, str]:
        """
        Shared time-horizon + price filter for all strategies.

        Rules:
          1. Entry price must be <= max_entry_cents
          2. Market must close within max_close_days (hard cutoff)
          3. If market closes more than long_horizon_days out,
             entry must be <= long_horizon_max_cents
             — further out = need higher potential return = lower price

        Returns (passes: bool, reason: str)
        """
        # Rule 1: Hard price cap
        if entry_cents > max_entry_cents:
            return False, f"price {entry_cents}¢ > max {max_entry_cents}¢"

        if close_time_str:
            try:
                closes_at = datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
                days_out = (closes_at - datetime.now(timezone.utc)).total_seconds() / 86400

                # Rule 2: Hard time horizon cutoff
                if days_out > max_close_days:
                    return False, (
                        f"closes in {days_out:.1f}d — beyond {max_close_days}d max horizon"
                    )

                # Rule 3: Long-horizon price floor (within the window)
                if days_out > long_horizon_days and entry_cents > long_horizon_max_cents:
                    return False, (
                        f"closes in {days_out:.1f}d (>{long_horizon_days}d) "
                        f"but price {entry_cents}¢ > {long_horizon_max_cents}¢ max for long horizon"
                    )
            except Exception:
                pass

        return True, "ok"
