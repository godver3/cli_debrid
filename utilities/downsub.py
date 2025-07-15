#!/usr/bin/env python3
import os
import sys
import logging
import time
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
    from subliminal import download_best_subtitles, save_subtitles, region
    from subliminal.video import Video
    from babelfish import Language
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
    'ger': Language('deu'),
    'spa': Language('spa'),
    'ita': Language('ita'),
    'por': Language('por'),
    'dut': Language('nld'),
    'rus': Language('rus'),
    'chi': Language('zho'),
    'zho': Language('zho'),  # Alternative code for Chinese
    'jpn': Language('jpn'),
    'kor': Language('kor'),
}

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
        
        # Convert language codes to Language objects
        languages = []
        for lang_code in config.SUBTITLE_LANGUAGES:
            if lang_code in LANGUAGE_MAP:
                languages.append(LANGUAGE_MAP[lang_code])
            else:
                # Try to create Language object directly
                try:
                    languages.append(Language(lang_code))
                    logging.info(f"✅ Using language code: {lang_code}")
                except Exception as e:
                    logging.warning(f"⚠️ Unknown language code: {lang_code} - {e}")
        
        if not languages:
            logging.error("❌ No valid languages configured")
            return False
        
        # Configure in-memory cache for faster performance
        region.configure('dogpile.cache.memory')
        
        # Create video object from name only (much faster)
        logging.info(f"�� Processing: {video_path.name}")
        video = Video.fromname(video_path.name)
        video.path = original_path  # Set to original path so subtitles are saved alongside the symlink
        
        # Start timer
        start_time = time.time()
        
        # Download best subtitles for all configured languages
        logging.info(f"🔍 Searching for subtitles in languages: {[str(lang) for lang in languages]}")
        # Try with just opensubtitles first
        logging.info(f"🔍 Using provider: opensubtitles")
        subtitles = download_best_subtitles([video], set(languages), providers={'opensubtitles'})
        
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
                lang = str(sub.language)
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
        return False

def main(specific_file=None):
    """
    Main function that processes a single video file using simplified name-only parsing.
    
    Args:
        specific_file (str, optional): Path to a specific file to process. Required.
    """
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