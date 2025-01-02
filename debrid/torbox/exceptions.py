"""TorBox API exceptions"""

class TorBoxAPIError(Exception):
    """Base exception for TorBox API errors"""
    pass

class TorBoxAuthError(TorBoxAPIError):
    """Authentication error with TorBox API"""
    pass

class TorBoxPlanError(TorBoxAPIError):
    """Plan restriction error with TorBox API"""
    pass

class TorBoxLimitError(TorBoxAPIError):
    """Rate limit or download limit error with TorBox API"""
    pass
