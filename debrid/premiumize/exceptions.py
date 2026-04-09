"""Premiumize-specific exceptions"""

from ..base import DebridProviderError


class PremiumizeError(DebridProviderError):
    """Base exception for Premiumize errors"""
    pass


class PremiumizeAPIError(PremiumizeError):
    """Exception raised for API-level errors"""
    pass


class PremiumizeAuthError(PremiumizeError):
    """Exception raised for authentication errors"""
    pass
