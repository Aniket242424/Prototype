"""Domain exceptions. Catching these is fine; catching `Exception` broadly is not."""
from __future__ import annotations


class TradingAgentError(Exception):
    """Base for all domain exceptions."""


# --- Auth ---
class AuthError(TradingAgentError):
    pass


class TokenExpiredError(AuthError):
    pass


class TokenStorageError(AuthError):
    pass


# --- Risk ---
class RiskRejection(TradingAgentError):
    """Raised when the Risk Engine declines a trade. Carries a structured reason."""

    def __init__(self, reason: str, code: str, details: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.details = details or {}


class KillSwitchTripped(RiskRejection):
    def __init__(self, reason: str = "kill switch active"):
        super().__init__(reason=reason, code="KILL_SWITCH")


class LiveTradingNotAuthorized(RiskRejection):
    def __init__(self, reason: str):
        super().__init__(reason=reason, code="LIVE_NOT_AUTHORIZED")


# --- Execution ---
class ExecutionError(TradingAgentError):
    pass


class SlippageExceededError(ExecutionError):
    pass


class BrokerError(ExecutionError):
    pass


# --- Market data ---
class MarketDataError(TradingAgentError):
    pass


class StaleDataError(MarketDataError):
    pass
