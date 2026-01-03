"""AllDebrid-specific exceptions"""

from ..base import DebridProviderError


class AllDebridError(DebridProviderError):
    """Base exception for AllDebrid errors"""
    pass


class AllDebridAPIError(AllDebridError):
    """Exception raised for API-level errors"""
    pass


class AllDebridAuthError(AllDebridError):
    """Exception raised for authentication errors"""
    pass
