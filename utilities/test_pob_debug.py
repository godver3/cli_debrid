#!/usr/bin/env python3
import os
import sys
import logging
from pathlib import Path

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config

# Import subliminal components
try:
    from subliminal import download_best_subtitles, region
    from subliminal.video import Video
    from babelfish import Language
except ImportError as e:
    logging.error(f"Required subliminal packages not installed: {e}")
    logging.error("Please install: pip install subliminal babelfish")
    sys.exit(1)

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def test_language_mapping():
    """Test the language mapping and expansion"""
    print("=== Testing Language Mapping ===")
    
    # Test the expand_languages function from downsub.py
    def expand_languages(codes):
        """Turn config codes into babelfish Languages, expanding 'por' to BR/PT."""
        out = []
        for code in codes:
            c = code.strip()
            # support IETF like 'pt-BR'
            try:
                if '-' in c:
                    out.append(Language.fromietf(c))
                    continue
            except Exception:
                pass
            if c in ('por', 'pt'):
                out.extend([Language('por'), Language('por','BR'), Language('por','PT')])
                continue
            if c in LANGUAGE_MAP:
                out.append(LANGUAGE_MAP[c])
                continue
            # last resort: try direct
            try:
                out.append(Language(c))
            except Exception as e:
                logging.warning(f"⚠️ Unknown language code: {c} - {e}")
        return out

    # Language mapping from config codes to babelfish Language objects
    LANGUAGE_MAP = {
        'ara': Language('ara'),
        'eng': Language('eng'), 
        'fre': Language('fra'),
        'ger': Language('deu'),
        'spa': Language('spa'),
        'ita': Language('ita'),
        'por': Language('por'),         # generic (keep if you want EU-PT too)
        'pt-BR': Language.fromietf('pt-BR'),
        'pob': Language('por', 'BR'),   # OpenSubtitles legacy code
        'pb': Language('por', 'BR'),    # common alias
        'dut': Language('nld'),
        'rus': Language('rus'),
        'chi': Language('zho'),
        'zho': Language('zho'),  # Alternative code for Chinese
        'jpn': Language('jpn'),
        'kor': Language('kor'),
    }

    # Test different language codes
    test_codes = ['pt-BR', 'pob', 'por', 'pb']
    
    for code in test_codes:
        print(f"\nTesting code: {code}")
        try:
            if code in LANGUAGE_MAP:
                lang = LANGUAGE_MAP[code]
                print(f"  Direct mapping: {lang}")
                print(f"  IETF: {getattr(lang, 'ietf', 'N/A')}")
                print(f"  Alpha2: {getattr(lang, 'alpha2', 'N/A')}")
                print(f"  Alpha3: {getattr(lang, 'alpha3', 'N/A')}")
                print(f"  Country: {getattr(lang, 'country', 'N/A')}")
            else:
                print(f"  Not in LANGUAGE_MAP")
        except Exception as e:
            print(f"  Error: {e}")
    
    # Test expand_languages function
    print(f"\n=== Testing expand_languages function ===")
    expanded = expand_languages(['pt-BR', 'pob', 'por'])
    for lang in expanded:
        print(f"  Expanded: {lang}")
        print(f"    IETF: {getattr(lang, 'ietf', 'N/A')}")
        print(f"    Alpha2: {getattr(lang, 'alpha2', 'N/A')}")
        print(f"    Alpha3: {getattr(lang, 'alpha3', 'N/A')}")
        print(f"    Country: {getattr(lang, 'country', 'N/A')}")

def test_subliminal_search():
    """Test subliminal search with different language configurations"""
    print("\n=== Testing Subliminal Search ===")
    
    video_path = "/media/Movies/How to Train Your Dragon (2025)/How to Train Your Dragon (2025) - tt26743210 - 1080p - (How.to.Train.Your.Dragon.2025.720p.AMZN.WEB-DLRip.H264_il68k).mkv"
    
    if not os.path.exists(video_path):
        print(f"Video file not found: {video_path}")
        return
    
    # Create video object
    video = Video.fromname(Path(video_path).name)
    video.path = video_path
    
    # Test different language configurations
    test_configs = [
        ['pt-BR'],
        ['pob'],
        ['por'],
        ['pt-BR', 'pob'],
        ['pt-BR', 'por'],
    ]
    
    for i, lang_codes in enumerate(test_configs):
        print(f"\n--- Test {i+1}: {lang_codes} ---")
        
        # Convert to Language objects
        languages = []
        for code in lang_codes:
            try:
                if '-' in code:
                    languages.append(Language.fromietf(code))
                elif code == 'pob':
                    languages.append(Language('por', 'BR'))
                elif code == 'por':
                    languages.append(Language('por'))
                else:
                    languages.append(Language(code))
            except Exception as e:
                print(f"  Error creating Language for {code}: {e}")
                continue
        
        print(f"  Languages: {languages}")
        
        try:
            # Configure cache
            region.configure('dogpile.cache.memory', replace_existing_backend=True)
            
            # Search for subtitles
            subtitles = download_best_subtitles([video], set(languages), providers={'opensubtitles'})
            
            if subtitles[video]:
                print(f"  ✅ Found {len(subtitles[video])} subtitle(s):")
                for sub in subtitles[video]:
                    lang_info = getattr(sub.language, 'ietf', None) or str(sub.language)
                    print(f"    - {sub} [{lang_info}]")
            else:
                print(f"  ❌ No subtitles found")
                
        except Exception as e:
            print(f"  ❌ Error: {e}")

def test_provider_config():
    """Test provider configuration"""
    print("\n=== Testing Provider Configuration ===")
    
    # Test the build_provider_configs function
    def build_provider_configs():
        pc = {}
        if config.OPENSUBTITLES_USERNAME and config.OPENSUBTITLES_PASSWORD:
            pc['opensubtitles'] = {
                'username': config.OPENSUBTITLES_USERNAME,
                'password': config.OPENSUBTITLES_PASSWORD
            }
        # If you also have OpenSubtitles.com (new API) creds/apikey:
        if getattr(config, 'OSCOM_USERNAME', None) and getattr(config, 'OSCOM_PASSWORD', None):
            pc['opensubtitlescom'] = {
                'username': config.OSCOM_USERNAME,
                'password': config.OSCOM_PASSWORD,
                'apikey':   getattr(config, 'OSCOM_APIKEY', None)
            }
        return pc
    
    provider_configs = build_provider_configs()
    print(f"Provider configs: {provider_configs}")

if __name__ == "__main__":
    test_language_mapping()
    test_subliminal_search()
    test_provider_config() 