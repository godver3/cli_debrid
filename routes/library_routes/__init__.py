"""Library Routes - Media library browser with infinite scroll
Fast visual browser for collected media"""

# Split from one large module; the old import path still resolves every name.
from flask import Blueprint, Response, jsonify, render_template, request
from flask_login import current_user
from ..models import admin_required, onboarding_required, user_required
from database.core import get_db_connection
from utilities.settings import get_nas_paths, get_setting
from debrid import get_debrid_provider
import logging
import requests
from datetime import datetime, timedelta
from ..discover_routes import get_certification, get_digital_release_date
from utilities.tmdb_cache import get_tmdb_json
from concurrent.futures import ThreadPoolExecutor
from ..poster_cache import load_cache, save_cache
from ._common import (
    ITEMS_PER_BATCH,
    MAX_BATCH_SIZE,
    library_bp,
)
from .posters import (
    _plex_image_session,
    cache_status,
    debug_posters,
    fetch_poster,
    fetch_poster_imdb,
    plex_image_proxy,
)
from .browse import (
    format_library_item,
    get_library_data,
    index,
    parse_sort,
)
from .show_detail import (
    _SkipQueueMissing,
    check_broken_files,
    move_missing_to_wanted,
    refresh_show_metadata,
    show_detail,
    show_detail_data,
)
from .movie_metadata import (
    clear_cache,
    clear_movie_release_date_override_route,
    get_trailer,
    refresh_movie_metadata,
    refresh_movie_release_date_route,
    set_movie_release_date_override_route,
)
from .deletion import (
    check_deletion_impact,
    delete_items,
    delete_show,
)
from .deletion_movie import (
    delete_movie,
    delete_movie_files,
)
from .deletion_season import (
    cancel_movie_replace,
    cancel_season_replace,
    delete_episode,
    delete_season,
    mark_movie_replace,
    mark_season_replace,
)
from .movie_detail import (
    add_not_wanted_magnet,
    get_cast,
    movie_detail,
    movie_detail_data,
)
from .subtitles import (
    download_subtitles_item,
    download_subtitles_season,
    download_subtitles_show,
)
from .fix_match import (
    FIX_MATCH_MOVIE_TYPES,
    FIX_MATCH_SHOW_TYPES,
    _fix_match_normalize_ids,
    _fix_match_refresh_entry_metadata,
    _fix_match_rematch_media_server,
    _fix_match_row_filter,
    _fix_match_types,
    fix_match_apply,
    fix_match_preview,
    fix_match_search,
)

__all__ = [
    'Blueprint',
    'FIX_MATCH_MOVIE_TYPES',
    'FIX_MATCH_SHOW_TYPES',
    'ITEMS_PER_BATCH',
    'MAX_BATCH_SIZE',
    'Response',
    'ThreadPoolExecutor',
    '_SkipQueueMissing',
    '_fix_match_normalize_ids',
    '_fix_match_refresh_entry_metadata',
    '_fix_match_rematch_media_server',
    '_fix_match_row_filter',
    '_fix_match_types',
    '_plex_image_session',
    'add_not_wanted_magnet',
    'admin_required',
    'cache_status',
    'cancel_movie_replace',
    'cancel_season_replace',
    'check_broken_files',
    'check_deletion_impact',
    'clear_cache',
    'clear_movie_release_date_override_route',
    'current_user',
    'datetime',
    'debug_posters',
    'delete_episode',
    'delete_items',
    'delete_movie',
    'delete_movie_files',
    'delete_season',
    'delete_show',
    'download_subtitles_item',
    'download_subtitles_season',
    'download_subtitles_show',
    'fetch_poster',
    'fetch_poster_imdb',
    'fix_match_apply',
    'fix_match_preview',
    'fix_match_search',
    'format_library_item',
    'get_cast',
    'get_certification',
    'get_db_connection',
    'get_debrid_provider',
    'get_digital_release_date',
    'get_library_data',
    'get_nas_paths',
    'get_setting',
    'get_tmdb_json',
    'get_trailer',
    'index',
    'jsonify',
    'library_bp',
    'load_cache',
    'logging',
    'mark_movie_replace',
    'mark_season_replace',
    'move_missing_to_wanted',
    'movie_detail',
    'movie_detail_data',
    'onboarding_required',
    'parse_sort',
    'plex_image_proxy',
    'refresh_movie_metadata',
    'refresh_movie_release_date_route',
    'refresh_show_metadata',
    'render_template',
    'request',
    'requests',
    'save_cache',
    'set_movie_release_date_override_route',
    'show_detail',
    'show_detail_data',
    'timedelta',
    'user_required',
]
