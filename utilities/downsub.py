#!/usr/bin/env python3
import os
import sys
import logging
from pathlib import Path

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
    from .subsource_downloader import SubSourceDownloader
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config
    from subsource_downloader import SubSourceDownloader

# Logging configuration
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)

# Language mapping from config codes to SubSource language names
LANGUAGE_MAP = {
    'ara': 'Arabic',
    'eng': 'English', 
    'fre': 'French',
    'ger': 'German',
    'spa': 'Spanish',
    'ita': 'Italian',
    'por': 'Portuguese',
    'dut': 'Dutch',
    'rus': 'Russian',
    'chi': 'Chinese BG code',
    'jpn': 'Japanese',
    'kor': 'Korean',
    # Add more mappings as needed
}

def main(specific_file=None):
    """
    Main function that processes a single video file using SubSource API.
    
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

    # Check if the file exists and is a valid video file (handle symlinks)
    if not os.path.exists(specific_file):
        logging.error(f"File does not exist: {specific_file}")
        return
    
    # Check if it's a symlink and log the target
    if os.path.islink(specific_file):
        target = os.readlink(specific_file)
        real_path = os.path.realpath(specific_file)
        logging.info(f"🔗 Processing symlink: {specific_file} -> {target}")
        logging.info(f"🔗 Real path: {real_path}")
        
        # Check if the symlink target exists
        if not os.path.exists(real_path):
            logging.error(f"Broken symlink - target does not exist: {real_path}")
            return
    
    # Use os.path.exists instead of os.path.isfile to handle symlinks better
    if not (os.path.isfile(specific_file) or (os.path.islink(specific_file) and os.path.exists(specific_file))):
        logging.error(f"File is not a valid file: {specific_file}")
        return

    if not specific_file.lower().endswith(config.VIDEO_EXTENSIONS):
        logging.error(f"File is not a valid video file: {specific_file}")
        return

    # Initialize SubSource downloader
    downloader = SubSourceDownloader()
    
    # Download subtitles for each configured language
    successful_downloads = 0
    total_duration = 0
    
    for lang_code in config.SUBTITLE_LANGUAGES:
        language_name = LANGUAGE_MAP.get(lang_code, lang_code)
        
        logging.info(f"Downloading {language_name} ({lang_code}) subtitles...")
        
        try:
            success, subtitle_path, duration = downloader.download_for_video(
                specific_file, 
                language=language_name, 
                language_code=lang_code
            )
            
            total_duration += duration
            
            if success:
                successful_downloads += 1
                logging.info(f"✅ {language_name} subtitle downloaded: {subtitle_path}")
            else:
                logging.warning(f"❌ Failed to download {language_name} subtitle")
                
        except Exception as e:
            logging.error(f"🚨 Error downloading {language_name} subtitle: {e}")
    
    # Log summary
    if successful_downloads > 0:
        logging.info(f"✅ Successfully downloaded {successful_downloads}/{len(config.SUBTITLE_LANGUAGES)} subtitles in {total_duration:.2f} seconds")
        logging.info(f"✅ Successfully processed: {specific_file}")
    else:
        logging.error(f"🚨 Failed to download any subtitles for: {specific_file}")

if __name__ == "__main__":
    # Check if a specific file path is provided as a command-line argument
    if len(sys.argv) > 1:
        main(specific_file=sys.argv[1])
    else:
        logging.error("Usage: python downsub.py <video_file>")
        sys.exit(1)