from ..base import DebridProviderError

class RealDebridError(DebridProviderError):
    """Base exception class for Real-Debrid specific errors"""
    pass

class RealDebridAPIError(RealDebridError):
    """Exception raised when the Real-Debrid API returns an error"""
    pass

class RealDebridAuthError(RealDebridError):
    """Exception raised when there are authentication issues"""
    pass
