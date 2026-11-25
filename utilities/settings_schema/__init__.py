"""Settings schema: every settings section, its fields, types and defaults.

Sections live in one file per group; SETTINGS_SCHEMA merges them in the
original order (the settings UI renders sections in this order)."""
from ._logos import AVAILABLE_LOGOS, get_available_logos
from . import accounts
from . import builtin_mount
from . import services
from . import queue_scraping
from . import debug
from . import scrapers
from . import content_sources
from . import features

SETTINGS_SCHEMA = {
    **accounts.SECTIONS,
    **builtin_mount.SECTIONS,
    **services.SECTIONS,
    **queue_scraping.SECTIONS,
    **debug.SECTIONS,
    **scrapers.SECTIONS,
    **content_sources.SECTIONS,
    **features.SECTIONS,
}

__all__ = ['SETTINGS_SCHEMA', 'AVAILABLE_LOGOS', 'get_available_logos']
