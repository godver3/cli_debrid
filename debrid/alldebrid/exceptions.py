from ..base import DebridProviderError

class AllDebridError(DebridProviderError):
    """Base exception class for AllDebrid specific errors"""
    pass

class AllDebridAPIError(AllDebridError):
    """Exception raised when the AllDebrid API returns an error"""
    pass

class AllDebridAuthError(AllDebridError):
    """Exception raised when there are authentication issues"""
    pass
