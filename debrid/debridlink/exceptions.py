"""Debrid-Link specific exceptions."""

from ..base import DebridProviderError


class DebridLinkError(DebridProviderError):
    """Base exception for Debrid-Link errors."""


class DebridLinkAPIError(DebridLinkError):
    """Raised when the Debrid-Link API returns an error."""


class DebridLinkAuthError(DebridLinkError):
    """Raised when Debrid-Link authentication fails."""
