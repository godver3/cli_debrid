import hashlib
import base64
from cryptography.fernet import Fernet

class _c:
    """Internal descriptor for capability protection"""
    
    def __init__(self, n: str):
        self.n = n
        self.c = f"_{hashlib.sha256(n.encode()).hexdigest()[:8]}"
    
    def __get__(self, i, o=None):
        if i is None:
            return self
        if not hasattr(i, self.c):
            v = i._get_capability_value(self.n)
            i.__dict__[self.c] = v
        return i.__dict__[self.c]
    
    def __set__(self, i, v):
        raise AttributeError("x")

# Encrypted capability values
_v = {
    'RealDebridProvider': {
        'direct_cache': b'gAAAAABnfA_Klql2nrRmlghouiZSvCczXQj2icYQtzF9MkIsyZtJnKzIwB-LUz8kOFMD1VwmKvTgLrkt6fZMujlqg1ahbzBjKQ==',
        'bulk_cache': b'gAAAAABnfA_K-1UH1Ca0rAeyoJqM-QN4HVMoxSQcl1oRYbXQ6H8g-IpXVyPW6EJpyJaoeW6-igDVetjl32gncwXJupsR7PJRgA==',
        'supports_uncached': b'gAAAAABnfA_Kt_BI1l3hWU4xwCXwuY3owcWYzkHHQVo5cs6QcC7e3Q3T4H8aqwDFDzlckIsddvJaQdbHG2G8mfPZQC0YFIS8NA=='
    },
    'TorBoxProvider': {
        'direct_cache': b'gAAAAABnfA_KSKia0DvLsMRF0oNsIyPsjreywWDTE1N6btAIedNsRcPZu6hGB3KWjjsxNmDJcCNeqOIHz46RBE7yvuM-Nj5qfA==',
        'bulk_cache': b'gAAAAABnfA_KWPvhsht2gqcWF3ijCOUNC2JJy_2-fyMRVd973b9-iTm-BcxbT4GdnGovyDqqf6FG1s1ma4rT8oCxKLLqmNUvqQ==',
        'supports_uncached': b'gAAAAABnfA_K-9PJz7wBeYBn4txmdVj8-4YMawJyID9mZfKnz7VS62pFpJ9Y1JXZOOVxiLsLoKiZWFB4ZPehochA5hduHbENpg=='
    }
}

# Property descriptors
_p1 = _c('direct_cache')
_p2 = _c('bulk_cache')
_p3 = _c('supports_uncached') 