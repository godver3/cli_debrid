"""Torbox-specific exceptions."""

from ..base import DebridProviderError


class TorboxError(DebridProviderError):
    """Base exception for Torbox errors."""


class TorboxAPIError(TorboxError):
    """Raised when the Torbox API returns an error."""


class TorboxAuthError(TorboxError):
    """Raised when Torbox authentication fails."""
