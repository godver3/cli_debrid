#!/usr/bin/env python3
import os
import sys
import logging
import time
from pathlib import Path
from functools import wraps

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config

# Import subliminal components
try:
    from subliminal import download_best_subtitles, save_subtitles, region
    from subliminal.video import Video
    from babelfish import Language
    import xml.parsers.expat
except ImportError as e:
    logging.error(f"Required subliminal packages not installed: {e}")
    logging.error("Please install: pip install subliminal babelfish")
    sys.exit(1)

# Logging configuration
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)

# Language mapping from config codes to babelfish Language objects
LANGUAGE_MAP = {
    'ara': Language('ara'),
    'eng': Language('eng'), 
    'fre': Language('fra'),
    'fra': Language('fra'),  # Add fra mapping for consistency
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

def setup_subliminal_credentials():
    """
    Configure subliminal with OpenSubtitles credentials if available
    """
    if config.OPENSUBTITLES_USERNAME and config.OPENSUBTITLES_PASSWORD:
        try:
            from subliminal.providers.opensubtitles import OpenSubtitlesProvider
            # Configure the provider with credentials
            OpenSubtitlesProvider.username = config.OPENSUBTITLES_USERNAME
            OpenSubtitlesProvider.password = config.OPENSUBTITLES_PASSWORD
            logging.info("✅ OpenSubtitles credentials configured")
            return True
        except Exception as e:
            logging.warning(f"⚠️ Failed to configure OpenSubtitles credentials: {e}")
            return False
    else:
        logging.info("ℹ️ No OpenSubtitles credentials found - using anonymous access")
        return False

def retry_on_xml_error(max_retries=3, delay=2):
    """Decorator to retry function calls that might fail due to XML parsing errors"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except xml.parsers.expat.ExpatError as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = delay * (attempt + 1)
                        logging.warning(f"XML parsing error (attempt {attempt + 1}/{max_retries}): {e}")
                        logging.warning(f"This usually indicates OpenSubtitles returned HTML instead of XML")
                        logging.warning(f"Waiting {wait_time} seconds before retry...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logging.error(f"Failed after {max_retries} attempts with XML parsing error: {e}")
                        break
                except Exception as e:
                    # Re-raise non-XML errors immediately
                    raise e
            
            # If we get here, all retries failed
            raise last_exception
        return wrapper
    return decorator

@retry_on_xml_error(max_retries=3, delay=3)
def download_subtitles_with_provider(video, languages, provider_name, provider_configs=None):
    """Download subtitles with a specific provider with retry logic"""
    logging.info(f"🔍 Using provider: {provider_name}")
    
    try:
        subtitles = download_best_subtitles([video], set(languages), providers={provider_name}, provider_configs=provider_configs)
        return subtitles
    except Exception as e:
        logging.error(f"Provider {provider_name} failed: {str(e)}")
        if "xml.parsers.expat.ExpatError" in str(e):
            logging.error("This is likely due to OpenSubtitles returning HTML instead of XML")
            logging.error("This can happen due to:")
            logging.error("- Server overload or maintenance")
            logging.error("- Rate limiting")
            logging.error("- Network connectivity issues")
        raise

def download_subtitles_for_video(video_path):
    """
    Download subtitles for a single video file using name-only parsing
    
    Args:
        video_path (str): Path to the video file
        
    Returns:
        bool: True if any subtitles were downloaded successfully
    """
    try:
        video_path = Path(video_path)
        
        # Check if file exists
        if not video_path.exists():
            logging.error(f"❌ File does not exist: {video_path}")
            return False
            
        # Check if it's a valid video file
        if not str(video_path).lower().endswith(config.VIDEO_EXTENSIONS):
            logging.error(f"❌ Not a valid video file: {video_path}")
            return False
        
        # Handle symlinks - keep track of both paths
        original_path = video_path
        if video_path.is_symlink():
            real_path = video_path.resolve()
            logging.info(f"🔗 Processing symlink: {video_path} -> {real_path}")
            video_path = real_path
        else:
            original_path = video_path
        
        # Convert language codes to Language objects using expand_languages
        languages = expand_languages(config.SUBTITLE_LANGUAGES)
        
        if not languages:
            logging.error("❌ No valid languages configured")
            return False
        
        # Configure in-memory cache for faster performance (only if not already configured)
        try:
            region.configure('dogpile.cache.memory', replace_existing_backend=True)
        except Exception:
            # Region already configured or other cache setup issue, which is fine
            pass
        
        # Create video object from symlink name (more information for parsing)
        symlink_name = original_path.name
        logging.info(f"🎬 Processing: {symlink_name}")
        video = Video.fromname(symlink_name)
        video.path = original_path  # Set to original path so subtitles are saved alongside the symlink
        
        # Build provider configurations
        provider_configs = build_provider_configs()
        
        # Start timer
        start_time = time.time()
        
        # Download best subtitles for all configured languages
        logging.info(f"🔍 Searching for subtitles in languages: {[getattr(lang, 'ietf', None) or str(lang) for lang in languages]}")
        
        # Try OpenSubtitles with retry logic
        try:
            subtitles = download_subtitles_with_provider(video, languages, 'opensubtitles', provider_configs)
        except Exception as e:
            logging.error(f"OpenSubtitles provider failed completely: {e}")
            logging.warning("You may want to try:")
            logging.warning("1. Checking your network connection")
            logging.warning("2. Waiting a few minutes and trying again")
            logging.warning("3. Using alternative subtitle providers")
            logging.warning("4. Checking OpenSubtitles.org status")
            
            # Try alternative providers as fallback
            alternative_providers = ['opensubtitlescom', 'podnapisi', 'tvsubtitles']
            logging.info("🔄 Attempting fallback providers...")
            
            for alt_provider in alternative_providers:
                try:
                    logging.info(f"🔍 Trying alternative provider: {alt_provider}")
                    subtitles = download_subtitles_with_provider(video, languages, alt_provider, provider_configs)
                    if subtitles[video]:
                        logging.info(f"✅ Success with alternative provider: {alt_provider}")
                        break
                except Exception as alt_e:
                    logging.warning(f"⚠️  Alternative provider {alt_provider} also failed: {alt_e}")
                    continue
            else:
                # If we get here, all providers failed
                logging.error("❌ All subtitle providers failed")
                return False
        
        # If we didn't get subtitles for all requested languages, try searching each language individually
        if subtitles[video]:
            found_languages = {str(sub.language) for sub in subtitles[video]}
            requested_languages = {str(lang) for lang in languages}
            missing_languages = requested_languages - found_languages
            
            if missing_languages:
                logging.info(f"⚠️  Missing subtitles for languages: {missing_languages}")
                logging.info("🔄 Trying individual language searches to ensure all requested languages are found...")
                
                # Try to find missing languages individually
                for missing_lang in missing_languages:
                    try:
                        # Find the Language object for the missing language
                        missing_lang_obj = None
                        for lang in languages:
                            if str(lang) == missing_lang:
                                missing_lang_obj = lang
                                break
                        
                        if missing_lang_obj:
                            logging.info(f"🔍 Searching individually for: {missing_lang}")
                            individual_subtitles = download_subtitles_with_provider(video, {missing_lang_obj}, 'opensubtitles', provider_configs)
                            
                            if individual_subtitles[video]:
                                # Add the found subtitles to our main results
                                subtitles[video].extend(individual_subtitles[video])
                                logging.info(f"✅ Found {len(individual_subtitles[video])} subtitle(s) for {missing_lang}")
                            else:
                                logging.warning(f"❌ Still no subtitles found for {missing_lang}")
                    except Exception as e:
                        logging.warning(f"⚠️  Failed to search individually for {missing_lang}: {e}")
                        continue
        
        # Stop timer
        elapsed_time = time.time() - start_time
        logging.info(f"⏱️ Subtitle search took {elapsed_time:.2f} seconds")
        
        # Check results and save subtitles
        if subtitles[video]:
            logging.info(f"✅ Found {len(subtitles[video])} subtitle(s): {subtitles[video]}")
            # --- Manually save subtitles to symlink directory ---
            symlink_dir = original_path.parent
            base_name = original_path.stem
            for sub in subtitles[video]:
                lang = getattr(sub.language, 'ietf', None) or str(sub.language)
                symlink_srt = symlink_dir / f"{base_name}.{lang}.srt"
                try:
                    with open(symlink_srt, 'wb') as f:
                        f.write(sub.content)
                    logging.info(f"Saved subtitle: {symlink_srt}")
                except Exception as e:
                    logging.error(f"Failed to save subtitle {symlink_srt}: {e}")
            logging.info(f"💾 Subtitles now in: {symlink_dir}")
            return True
        else:
            logging.warning("❌ No subtitles found")
            return False
            
    except Exception as e:
        logging.error(f"❌ Error downloading subtitles: {e}")
        if "xml.parsers.expat.ExpatError" in str(e):
            logging.error("💡 Troubleshooting tips for XML parsing errors:")
            logging.error("   - This usually means OpenSubtitles returned HTML instead of XML")
            logging.error("   - Try running the command again in a few minutes")
            logging.error("   - Check OpenSubtitles.org status in your browser")
            logging.error("   - Consider using alternative subtitle sources")
        return False

def main(specific_file=None):
    """
    Main function that processes a single video file using simplified name-only parsing.
    
    Args:
        specific_file (str, optional): Path to a specific file to process. Required.
    """
    # Reload configuration to pick up any changes
    config.reload()
    
    # Skip everything if subtitles are not enabled
    if not config.SUBTITLES_ENABLED:
        logging.info("Subtitle downloading is disabled in settings")
        return

    # Require a specific file
    if not specific_file:
        logging.error("No specific file provided")
        return

    # Setup credentials
    setup_subliminal_credentials()
    
    # Download subtitles
    if download_subtitles_for_video(specific_file):
        logging.info(f"✅ Successfully processed: {specific_file}")
    else:
        logging.error(f"🚨 Failed to download subtitles for: {specific_file}")

if __name__ == "__main__":
    # Check if a specific file path is provided as a command-line argument
    if len(sys.argv) > 1:
        main(specific_file=sys.argv[1])
    else:
        logging.error("Usage: python downsub.py <video_file>")
        sys.exit(1)