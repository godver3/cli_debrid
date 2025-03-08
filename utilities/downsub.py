import os
import sys
import logging
import json
import re
from datetime import datetime, timedelta
from babelfish import Language
from subliminal import download_best_subtitles, scan_video
from subliminal.cache import region
from .config.downsub_config import (
    SUBTITLES_ENABLED, VIDEO_FOLDERS, SCAN_CACHE_FILE, DIR_CACHE_FILE,
    LOG_LEVEL, LOG_FORMAT, LOG_FILE, VIDEO_EXTENSIONS,
    SUBTITLE_LANGUAGES, SUBLIMINAL_USER_AGENT, SUBTITLE_PROVIDERS, ONLY_CURRENT_FILE
)

# Configure global in-memory cache for subliminal
region.configure("dogpile.cache.memory")

# Logging configuration
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

def load_cache(filename):
    """Load cache from a JSON file."""
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load cache from {filename}: {e}")
    return {}

def save_cache(cache, filename):
    """Save cache to a JSON file."""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        logging.error(f"Failed to save cache to {filename}: {e}")

def scan_directory(dir_path, dir_cache, file_cache, ignore_dir_cache=False):
    """
    Recursively scan the directory while following symlinks for movie files.
    Uses a simple directory modification time to decide if the folder changed.
    """
    files_found = []
    # Use the directory's modification time as a simple signature.
    current_dir_sig = os.path.getmtime(dir_path)
    cached_dir_sig = dir_cache.get(dir_path)
    if not ignore_dir_cache and cached_dir_sig and cached_dir_sig == current_dir_sig:
        logging.info(f"📂 Skipping unchanged directory: {dir_path}")
        return files_found
    dir_cache[dir_path] = current_dir_sig

    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                # Preserve the original symlink path
                full_path = entry.path

                if entry.is_symlink():
                    real_path = os.path.realpath(entry.path)
                    logging.info(f"🔗 Detected symlink: {entry.path} -> {real_path}")

                # If it's a directory, follow it
                if entry.is_dir(follow_symlinks=True):
                    files_found.extend(scan_directory(entry.path, dir_cache, file_cache, ignore_dir_cache))
                # If it's a movie file, add it
                elif entry.is_file(follow_symlinks=True) and entry.name.lower().endswith(VIDEO_EXTENSIONS):
                    try:
                        # Use the symlink's metadata so the subtitle file is saved next to the symlink.
                        stat_info = os.stat(full_path, follow_symlinks=False)
                        file_cache[full_path] = {"mod_time": stat_info.st_mtime, "size": stat_info.st_size}
                        files_found.append(full_path)
                        logging.info(f"🎥 Found file: {full_path}")
                    except FileNotFoundError:
                        logging.warning(f"⚠️ Broken symlink, file not found: {entry.path}")
    except Exception as e:
        logging.error(f"🚨 Error scanning directory {dir_path}: {e}")

    return files_found

def download_subtitles(files):
    """Download subtitles for a list of files."""
    if not files:
        logging.info("✅ No files to process.")
        return

    languages = {Language(lang) for lang in SUBTITLE_LANGUAGES}
    os.environ['SUBLIMINAL_USER_AGENT'] = SUBLIMINAL_USER_AGENT

    videos = []
    for f in files:
        try:
            video = scan_video(f)
            videos.append((video, f))  # Keep the original file path (symlink)
        except ValueError as e:
            logging.error(f"⚠️ Could not parse file: {f} - {e}")

    if not videos:
        logging.warning("❌ No valid video files for subtitle download.")
        return

    try:
        # Download subtitles for the video objects.
        subtitles = download_best_subtitles(
            [v[0] for v in videos], 
            languages=languages, 
            providers=SUBTITLE_PROVIDERS
        )
        for video, original_path in videos:
            subs = subtitles.get(video, [])
            for sub in subs:
                # Save subtitle next to the symlink (using original file path)
                sub_path = os.path.splitext(original_path)[0] + f'.{sub.language}.srt'
                with open(sub_path, 'wb') as f:
                    f.write(sub.content)
                logging.info(f"📥 Downloaded subtitle: {sub_path}")
    except Exception as e:
        logging.error(f"🚨 Subtitle download failed: {e}")

def main(specific_file=None):
    """
    Main function that processes videos in the configured folders.
    Uses cache to track processed files and only downloads subtitles for new or modified files.
    
    Args:
        specific_file (str, optional): Path to a specific file to process. If provided and ONLY_CURRENT_FILE is True,
                                      only this file will be processed. Defaults to None.
    """
    # Skip everything if subtitles are not enabled
    if not SUBTITLES_ENABLED:
        logging.info("Subtitle downloading is disabled in settings")
        return

    # If we're only processing the current file and a specific file is provided
    if ONLY_CURRENT_FILE and specific_file:
        logging.info(f"Only processing specific file: {specific_file}")
        if os.path.isfile(specific_file) and specific_file.lower().endswith(VIDEO_EXTENSIONS):
            download_subtitles([specific_file])
        else:
            logging.warning(f"Specified file is not a valid video file: {specific_file}")
        return

    # Load caches
    file_cache = load_cache(SCAN_CACHE_FILE)
    dir_cache = load_cache(DIR_CACHE_FILE)

    files_to_process = []
    
    # Process each configured video folder
    for folder_path in VIDEO_FOLDERS:
        # Check if video folder exists
        if not os.path.isdir(folder_path):
            logging.error(f"❌ Invalid video folder path: {folder_path}")
            continue

        # Scan for video files in this folder
        folder_files = scan_directory(folder_path, dir_cache, file_cache, ignore_dir_cache=False)
        files_to_process.extend(folder_files)

    # Skip files that are already recorded and unchanged
    files_to_process = [
        f for f in files_to_process if f not in file_cache or file_cache[f]["mod_time"] != os.path.getmtime(f)
    ]

    logging.info(f"🔎 Found {len(files_to_process)} files to process.")
    download_subtitles(files_to_process)

    # Save updated caches
    save_cache(file_cache, SCAN_CACHE_FILE)
    save_cache(dir_cache, DIR_CACHE_FILE)

if __name__ == "__main__":
    main()