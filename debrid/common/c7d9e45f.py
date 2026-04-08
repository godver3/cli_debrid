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
    'AllDebridProvider': {
        'direct_cache': b'gAAAAABpWFBI5LpjoXHHD0M88gTWFJJWWF11I1WnE_R9QSKqdGH-SyKWb_T5GRqQdSRnsmckU2Tj34jDtXD_19qECoTcRaWI5A==',
        'bulk_cache': b'gAAAAABpWFBI-B0QZv1rn7ieePQt1aF2oEz82kz8xtXWFuTWotjEcgDNTi5SObW4gkTZC-Syosv-FpdZKcsfFp4uA6EJkfpBsQ==',
        'supports_uncached': b'gAAAAABpWFBICGPRNPbZGs_xgeF8eq78USs8nSgoZtkwbHN9lfQx-qI7Elm94EKrUFWoYNwu9x2dhNGZJ_PAu9T3FJds2QMHqg=='
    },
    'TorboxProvider': {
        'direct_cache': b'gAAAAABp1nrtFaMtmAKXLZcEzkIZGAD5v7dckYhGzjfzOwaLS8OEjupwll-4kWTm1Fi2GpnZj-Pf6smZ1YEz3mVsd_6E7YS4NQ==',
        'bulk_cache': b'gAAAAABp1nrtS2jcke2JzbNu5xhxikPpwq8TO65gJCAQhTc47WKOS4OsDD_6XL4Mmx4PPbMoPtRP3bupw439Pbz629xqhjIAEQ==',
        'supports_uncached': b'gAAAAABp1nrtnsL7O_ZId4yOMeQxUiP3mHA_dTJUI4OIkJCKTKEYVCTv09HgIxAiICr9M-xFe0u61jOHMaloQFcNnuFYgvSv5w=='
    }
}

# Property descriptors
_p1 = _c('direct_cache')
_p2 = _c('bulk_cache')
_p3 = _c('supports_uncached') 
