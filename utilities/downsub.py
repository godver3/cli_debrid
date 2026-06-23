#!/usr/bin/env python3
import os
import sys
import json
import logging
import subprocess
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
SUBLIMINAL_AVAILABLE = False
try:
    from subliminal import download_best_subtitles, save_subtitles, region
    from subliminal.video import Video
    from babelfish import Language
    import xml.parsers.expat
    SUBLIMINAL_AVAILABLE = True
except Exception as e:
    logging.warning(f"Subliminal packages not available: {e}")
    logging.warning("Subtitle downloading will be disabled. Install with: pip install subliminal babelfish")

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
# Only populated when babelfish is available
LANGUAGE_MAP = {}
if SUBLIMINAL_AVAILABLE:
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

def get_embedded_subtitle_languages(file_path: Path) -> set:
    """
    Use ffprobe to detect embedded subtitle track languages in a video file.
    Returns a set of ISO-639-2/3 language codes (lowercase) found as subtitle streams.
    Returns empty set if ffprobe is unavailable, times out, or the file is inaccessible.
    """
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-select_streams', 's',
                '-show_entries', 'stream_tags=language',
                '-of', 'json',
                str(file_path),
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return set()
        data = json.loads(result.stdout or '{}')
        langs = set()
        for stream in data.get('streams', []):
            lang = (stream.get('tags') or {}).get('language', '').strip().lower()
            if lang and lang not in ('und', 'unknown', ''):
                langs.add(lang)
        return langs
    except FileNotFoundError:
        logging.warning('ffprobe not found — skipping embedded subtitle probe')
        return set()
    except subprocess.TimeoutExpired:
        logging.warning(f'ffprobe timed out probing {file_path} — skipping embedded subtitle probe')
        return set()
    except Exception as e:
        logging.warning(f'ffprobe probe failed for {file_path}: {e}')
        return set()


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

def upload_subtitle_to_plex(rating_key, content, lang_code, fmt='srt'):
    """Upload a subtitle to a Plex item via the Plex API (no filesystem write).

    Used when the media lives on a read-only mount (Plex mode over a debrid/
    usenet rclone/WebDAV mount), where a sidecar .srt cannot be written. This is
    storage-agnostic — keyed on the Plex ratingKey — so it works identically for
    zurg, nzbdav, climount and any future backend. Returns True on success.
    """
    import requests as _rq
    from utilities.settings import get_setting
    plex_url = (get_setting('Plex', 'url', '') or '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '') or ''
    if not (plex_url and plex_token and rating_key):
        logging.warning(f"[Plex subs] missing url/token/ratingKey — cannot upload (rk={rating_key})")
        return False
    data = content if isinstance(content, bytes) else (content or '').encode('utf-8')
    if not data:
        return False
    try:
        url = f"{plex_url}/library/metadata/{rating_key}/subtitles"
        # Plex REJECTS state-changing POSTs with HTTP 500 unless an
        # X-Plex-Client-Identifier header is present. Language is detected by Plex
        # from the title's ".<lang>." token (the bare 'language' param alone is
        # unreliable), so encode it in the title too. Upload subtitles is a free
        # Plex feature (no Plex Pass required).
        params = {
            'title': f'subtitle.{lang_code}.{fmt}',
            'format': fmt,
            'language': lang_code,
            'X-Plex-Token': plex_token,
        }
        headers = {
            'Accept': 'text/plain, */*',
            'X-Plex-Client-Identifier': 'cli-debrid-subtitles',
            'X-Plex-Product': 'cli_debrid',
            'X-Plex-Version': '1.0',
        }
        r = _rq.post(url, params=params, data=data, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            logging.info(f"[Plex subs] uploaded {lang_code} subtitle to ratingKey {rating_key}")
            return True
        logging.warning(f"[Plex subs] upload failed rk={rating_key} lang={lang_code}: HTTP {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logging.error(f"[Plex subs] upload error rk={rating_key}: {e}")
        return False


def download_subtitles_for_video(video_path, rating_key=None, name_hint=None):
    """
    Download subtitles for a single video file using name-only parsing.

    Args:
        video_path (str): Path to the video file
        rating_key (str, optional): Plex ratingKey. When the media directory is
            NOT writable (read-only mount), subtitles are uploaded to this Plex
            item via API instead of being written as a sidecar.

    Returns:
        bool: True if any subtitles were downloaded/uploaded successfully
    """
    try:
        video_path = Path(video_path)

        from utilities.settings import get_setting
        is_plex_mode = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'

        # Check if file exists — skip for Plex mode (file is on FUSE mount, not local)
        if not is_plex_mode and not video_path.exists():
            logging.error(f"❌ File does not exist: {video_path}")
            return False

        # Check if it's a valid video file
        if not str(video_path).lower().endswith(config.VIDEO_EXTENSIONS):
            logging.error(f"❌ Not a valid video file: {video_path}")
            return False
        
        # Handle symlinks - keep track of both paths (skip in Plex mode)
        original_path = video_path
        if not is_plex_mode and video_path.is_symlink():
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

        # Probe for embedded subtitles if enabled — skip in Plex mode (file not locally accessible)
        if config.PROBE_FOR_EMBEDDED and not is_plex_mode:
            probe_path = video_path  # already resolved to real path above if symlink
            embedded = get_embedded_subtitle_languages(probe_path)
            if embedded:
                logging.info(f"🔍 Embedded subtitle languages detected: {embedded}")
                # Filter out languages already covered by an embedded track.
                # babelfish Language alpha3 (e.g. 'eng', 'fra') is compared against
                # ffprobe tags which are typically ISO-639-2 (also 3-letter).
                filtered = [
                    lang for lang in languages
                    if lang.alpha3 not in embedded and str(lang) not in embedded
                ]
                if not filtered:
                    logging.info("✅ All configured languages already embedded — skipping external search")
                    return True
                removed = len(languages) - len(filtered)
                if removed:
                    logging.info(f"ℹ️  Skipping {removed} language(s) already embedded; searching for remaining {len(filtered)}")
                languages = filtered

        # Configure in-memory cache for faster performance (only if not already configured)
        try:
            region.configure('dogpile.cache.memory', replace_existing_backend=True)
        except Exception:
            # Region already configured or other cache setup issue, which is fine
            pass
        
        # Create video object from the most descriptive name available. The raw
        # file on a debrid/usenet mount is often obfuscated (e.g. "tGcr.mkv"), so
        # callers pass name_hint = the release folder name for reliable matching.
        # In symlink mode the symlink filename is already descriptive.
        #
        # Prefer the actual filename over name_hint when the hint is a season-level
        # folder name (no SxxExx) but the file itself has episode info. This fixes
        # season packs where debrid_folder_name = "Show S01-S02 ..." which causes
        # subliminal's guessit parser to fail with "Insufficient data".
        import re as _re_sub
        _hint_has_ep = bool(_re_sub.search(r'[Ss]\d{2}[Ee]\d{2}', name_hint or ''))
        _file_has_ep = bool(_re_sub.search(r'[Ss]\d{2}[Ee]\d{2}', original_path.name))
        if name_hint and not _hint_has_ep and _file_has_ep:
            parse_name = original_path.name
            logging.debug(f"[subs] name_hint lacks episode info, using filename instead: {original_path.name}")
        else:
            parse_name = name_hint or original_path.name
        logging.info(f"🎬 Processing: {original_path.name} (parsing as: {parse_name})")
        video = Video.fromname(parse_name)
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
            symlink_dir = original_path.parent
            base_name = original_path.stem
            # Save mode is decided by WRITABILITY, not by provider:
            #   - media dir writable (symlink/local mode, Jellyfin/Emby, …)
            #       → write a sidecar .srt next to the file (media-server-neutral).
            #   - media dir NOT writable (read-only debrid/usenet mount in Plex mode)
            #       → upload to Plex via API keyed on rating_key (storage-agnostic;
            #         works the same for zurg/nzbdav/climount/future backends).
            can_write_sidecar = os.access(str(symlink_dir), os.W_OK)
            use_plex_upload = (not can_write_sidecar) and bool(rating_key)
            saved = 0
            for sub in subtitles[video]:
                lang = getattr(sub.language, 'ietf', None) or str(sub.language)
                content = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
                if not content:
                    continue
                if use_plex_upload:
                    lang3 = getattr(sub.language, 'alpha3', None) or lang
                    if upload_subtitle_to_plex(rating_key, content, lang3):
                        saved += 1
                elif can_write_sidecar:
                    symlink_srt = symlink_dir / f"{base_name}.{lang}.srt"
                    try:
                        with open(symlink_srt, 'wb') as f:
                            f.write(content)
                        logging.info(f"Saved subtitle: {symlink_srt}")
                        saved += 1
                    except Exception as e:
                        logging.error(f"Failed to save subtitle {symlink_srt}: {e}")
                else:
                    # Fallback: cli_mount sidecar injection
                    try:
                        from usenet.climount_client import get_climount_client
                        # cli_mount's inject API uses the job UUID (info_hash) as the folder
                        # identifier, not the human-readable folder title. Look it up from
                        # the DB via filled_by_file or location_on_disk matching.
                        torrent_name = symlink_dir.name  # fallback
                        try:
                            from database.core import get_db_connection as _get_dbc
                            _fname = original_path.name
                            _loc = str(original_path)
                            _conn = _get_dbc()
                            _row = _conn.execute(
                                "SELECT filled_by_torrent_id FROM media_items "
                                "WHERE state='Collected' AND ("
                                "  filled_by_file=? OR location_on_disk=? OR location_on_disk LIKE ?"
                                ") AND filled_by_torrent_id LIKE 'nzb:%' LIMIT 1",
                                (_fname, _loc, f'%{_fname}')
                            ).fetchone()
                            _conn.close()
                            if _row and _row[0]:
                                torrent_name = _row[0].replace('nzb:', '', 1)
                                logging.debug(f"[subs] Using job UUID for sidecar injection: {torrent_name}")
                        except Exception as _dbl:
                            logging.debug(f"[subs] Could not resolve job UUID from DB: {_dbl}")
                        srt_filename = f"{base_name}.{lang}.srt"
                        client = get_climount_client()
                        if client.inject_sidecar_file(torrent_name, srt_filename, content):
                            logging.info(f"[subs] Injected sidecar subtitle via cli_mount: {torrent_name}/{srt_filename}")
                            saved += 1
                        else:
                            logging.warning(f"[subs] {base_name}: cli_mount sidecar injection failed for {lang}")
                    except Exception as _dce:
                        logging.warning(f"[subs] {base_name}: media dir not writable, no Plex ratingKey, cli_mount injection failed: {_dce}")
            logging.info(f"💾 Saved/uploaded {saved}/{len(subtitles[video])} subtitle(s) for {base_name} "
                         f"({'Plex API' if use_plex_upload else 'sidecar' if can_write_sidecar else 'cli_mount sidecar'})")
            return saved > 0
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

def main(specific_file=None, rating_key=None, name_hint=None):
    """
    Main function that processes a single video file using simplified name-only parsing.

    Args:
        specific_file (str, optional): Path to a specific file to process. Required.
        rating_key (str, optional): Plex ratingKey — used to upload subtitles via
            the Plex API when the media directory is read-only (Plex mode).
        name_hint (str, optional): Descriptive release name used for subtitle
            matching when the on-disk filename is obfuscated (debrid/usenet mounts).
    """
    if not SUBLIMINAL_AVAILABLE:
        logging.warning("Subliminal not available, skipping subtitle download.")
        return

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
    if download_subtitles_for_video(specific_file, rating_key=rating_key, name_hint=name_hint):
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