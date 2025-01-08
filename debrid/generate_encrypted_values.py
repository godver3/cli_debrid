import base64
import hashlib
from cryptography.fernet import Fernet

def generate_encrypted_values():
    providers = {
        'RealDebridProvider': {
            'direct_cache': False, 
            'bulk_cache': False,
            'supports_uncached': True
        },
        'TorBoxProvider': {
            'direct_cache': True, 
            'bulk_cache': True,
            'supports_uncached': False
        }
    }
    
    for provider_name, capabilities in providers.items():
        print(f"\n{provider_name}:")
        # Generate key from provider name
        key_base = provider_name.encode() + b'debrid_capabilities_key'
        key = base64.urlsafe_b64encode(hashlib.sha256(key_base).digest()[:32])
        cipher = Fernet(key)
        
        for capability, value in capabilities.items():
            encrypted = cipher.encrypt(str(value).encode())
            print(f"'{capability}': {encrypted},")

if __name__ == '__main__':
    generate_encrypted_values() 