"""
Overlay System

Generates custom poster overlays with badges for resolution, HDR, audio codecs, etc.
Supports Plex, Jellyfin, and Emby.
"""

from .renderer import OverlayRenderer
from .plex_client import PlexClient
from .media_info import MediaInfoExtractor
from .overlay_manager import OverlayManager
from .layout_manager import LayoutManager
from .layout_validator import LayoutValidator
from .badge_manager import BadgeManager
from . import db_init
from . import element_definitions
from . import imdb_dataset

__all__ = [
    'OverlayRenderer',
    'PlexClient',
    'MediaInfoExtractor',
    'OverlayManager',
    'LayoutManager',
    'LayoutValidator',
    'BadgeManager',
    'db_init',
    'element_definitions',
    'imdb_dataset',
]
