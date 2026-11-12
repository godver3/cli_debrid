"""HighWay-specific exceptions."""

from ..base import DebridProviderError


class HighwayError(DebridProviderError):
    """Base exception for HighWay errors."""


class HighwayAPIError(HighwayError):
    """Raised when the HighWay API returns an error."""


class HighwayAuthError(HighwayError):
    """Raised when HighWay authentication fails (invalid token or missing scope)."""
