#!/usr/bin/env python3
import os
import sys
import logging
import subprocess
from pathlib import Path

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config

# Logging configuration
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)

def main(specific_file=None):
    """
    Main function that processes a single video file using downsub.sh.
    
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

    # Get the path to downsub.sh script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    downsub_sh_path = os.path.join(script_dir, 'downsub.sh')
    
    if not os.path.isfile(downsub_sh_path):
        logging.error(f"downsub.sh script not found at: {downsub_sh_path}")
        return

    # Build the command with file and languages
    cmd = [downsub_sh_path, specific_file] + config.SUBTITLE_LANGUAGES
    
    logging.info(f"Calling downsub.sh with: {' '.join(cmd)}")
    
    try:
        # Call the downsub.sh script
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        if result.stdout:
            logging.info(f"downsub.sh output: {result.stdout}")
        if result.stderr:
            logging.warning(f"downsub.sh stderr: {result.stderr}")
            
        logging.info(f"✅ Successfully processed: {specific_file}")
        
    except subprocess.CalledProcessError as e:
        logging.error(f"🚨 downsub.sh failed with exit code {e.returncode}")
        if e.stdout:
            logging.error(f"stdout: {e.stdout}")
        if e.stderr:
            logging.error(f"stderr: {e.stderr}")
    except Exception as e:
        logging.error(f"🚨 Error calling downsub.sh: {e}")

if __name__ == "__main__":
    # Check if a specific file path is provided as a command-line argument
    if len(sys.argv) > 1:
        main(specific_file=sys.argv[1])
    else:
        logging.error("Usage: python downsub.py <video_file>")
        sys.exit(1)