"""Content source types and their per-source fields."""

from . import content_source_types_trakt_mdblist, content_source_types_scrob_floppy_simkl, content_source_types_requests_plex, content_source_types_special

SECTIONS = {
    "Content Sources": {
        "tab": "Content Sources",
        "type": "dict",
        "description": "Content source configurations",
        "default": {},
        "schema": {
            **content_source_types_trakt_mdblist.TYPES,
            **content_source_types_scrob_floppy_simkl.TYPES,
            **content_source_types_requests_plex.TYPES,
            **content_source_types_special.TYPES,
        }
    },
}
