"""Logo choices offered in UI Settings."""
import os
import re
import glob


def get_available_logos():
    """
    Scan the static directory to find available logo files and categorize them.
    Returns a list of logo options with the format: ["Default", "Plex-Inspired"].
    """
    # Define the static directory path relative to this file
    static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    
    # Default logo options (always include these if assets exist)
    logo_options = ["Default", "Plex-Inspired", "Color Icon"]
    
    # Pattern matching for Plex-Inspired logo
    plex_pattern = re.compile(r"plex-icon-\d+x\d+\.(png|ico)$")
    
    # Check if the static directory exists
    if os.path.exists(static_dir):
        # Get all files in the static directory
        files = glob.glob(os.path.join(static_dir, "*.*"))
        
        # Check if Plex logo is available
        for file_path in files:
            filename = os.path.basename(file_path)
            if plex_pattern.search(filename):
                if "Plex-Inspired" not in logo_options:
                    logo_options.append("Plex-Inspired")
                break
    
    return logo_options


# Get available logo options dynamically
AVAILABLE_LOGOS = get_available_logos()
