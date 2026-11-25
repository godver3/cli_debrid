"""Debug blueprint and progress state shared by the debug route modules."""

from flask import Blueprint

debug_bp = Blueprint('debug', __name__)

# Global progress tracking
scan_progress = {}

# Global dictionary to store analysis progress
analysis_progress = {}

# Global dictionary for Rclone to Symlink progress tracking
rclone_scan_progress = {}
