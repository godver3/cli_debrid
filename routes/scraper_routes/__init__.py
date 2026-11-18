"""routes.scraper_routes (split into a package)."""

# Split from one large module; the old import path still resolves every name.
from flask import Blueprint, jsonify, render_template, request, session
import copy
import logging
from usenet import get_usenet_provider_display_name as _usenet_pname
from debrid import get_debrid_provider
from ..models import admin_required, onboarding_required, scraper_permission_required, scraper_view_access_required, user_required
from utilities.settings import get_all_settings, get_setting, load_config, save_config
from database.database_reading import get_all_season_episode_counts, get_media_item_presence_overall, get_media_items_presence_batch
from utilities.web_scraper import process_media_selection, process_torrent_selection, trending_anime, trending_movies, trending_shows, web_scrape_tvshow
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from utilities.web_scraper import search_trakt
from utilities.local_library_scan import extract_resolution_from_filename
from queues.torrent_processor import TorrentProcessor
from queues.media_matcher import MediaMatcher
from typing import Any, Dict, Tuple
import requests
import tempfile
import os
import bencodepy
import hashlib
from datetime import timezone
from database.torrent_tracking import get_torrent_history, record_torrent_addition, update_torrent_tracking
from flask_login import current_user
from ..utils import is_user_system_enabled
import asyncio
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
import re
import threading
import time
import json
from utilities.web_scraper import get_media_meta
from typing import List
import iso8601
from utilities.reverse_parser import parse_filename_for_version
from utilities.tmdb_cache import get_cached_db_statuses, get_from_cache, set_in_cache
from concurrent.futures import ThreadPoolExecutor
from ._common import (
    ContentProcessor,
    _download_and_get_hash,
    _phalanx_cache_manager,
    _record_manual_grab,
    _search_analytics,
    convert_tmdb_to_imdb,
    obfuscate_magnet_link,
    scraper_bp,
)
from .usenet_add import (
    _add_nzb_pack_to_usenet,
    _add_nzb_to_usenet,
    _build_debrid_title,
    _build_nzb_title,
    _get_content_source_display_name,
    _submit_single_episode_nzb,
)
from .add_to_debrid import (
    add_torrent_to_debrid,
)
from .trending import (
    _TRENDING_FRESH_SECONDS,
    _fetch_all_trending_now,
    _refresh_trending_in_background,
    _trending_lock,
    _trending_state,
    all_trending,
    anime_trending,
    fetch_all_trending_parallel,
    movies_trending,
    shows_trending,
)
from .search import (
    index,
    live_search,
    optimization_status,
    search_analytics,
    select_episode,
    select_media,
    select_season,
)
from .tester import (
    add_torrent,
    api_battery_search,
    get_item_details,
    get_media_meta_endpoint,
    get_tv_details,
    remove_uncached_item,
    run_scrape,
    scraper_tester,
    tmdb_image_proxy,
)
from .cache_lookup import (
    _fetch_details_for_id_lookup,
    check_cache_status,
    lookup_by_id,
)
from .files import (
    get_nzb_files,
    get_symlink_folders,
    get_torrent_files,
)

__all__ = [
    'Any',
    'Blueprint',
    'ContentProcessor',
    'Dict',
    'List',
    'MediaMatcher',
    'PhalanxDBClassManager',
    'ThreadPoolExecutor',
    'TorrentProcessor',
    'Tuple',
    '_TRENDING_FRESH_SECONDS',
    '_add_nzb_pack_to_usenet',
    '_add_nzb_to_usenet',
    '_build_debrid_title',
    '_build_nzb_title',
    '_download_and_get_hash',
    '_fetch_all_trending_now',
    '_fetch_details_for_id_lookup',
    '_get_content_source_display_name',
    '_phalanx_cache_manager',
    '_record_manual_grab',
    '_refresh_trending_in_background',
    '_search_analytics',
    '_submit_single_episode_nzb',
    '_trending_lock',
    '_trending_state',
    '_usenet_pname',
    'add_torrent',
    'add_torrent_to_debrid',
    'admin_required',
    'all_trending',
    'anime_trending',
    'api_battery_search',
    'asyncio',
    'bencodepy',
    'check_cache_status',
    'convert_tmdb_to_imdb',
    'copy',
    'current_user',
    'extract_resolution_from_filename',
    'fetch_all_trending_parallel',
    'get_all_season_episode_counts',
    'get_all_settings',
    'get_cached_db_statuses',
    'get_debrid_provider',
    'get_details',
    'get_from_cache',
    'get_item_details',
    'get_media_item_presence_overall',
    'get_media_items_presence_batch',
    'get_media_meta',
    'get_media_meta_endpoint',
    'get_nzb_files',
    'get_setting',
    'get_symlink_folders',
    'get_torrent_files',
    'get_torrent_history',
    'get_tv_details',
    'hashlib',
    'index',
    'is_user_system_enabled',
    'iso8601',
    'json',
    'jsonify',
    'live_search',
    'load_config',
    'logging',
    'lookup_by_id',
    'movies_trending',
    'obfuscate_magnet_link',
    'onboarding_required',
    'optimization_status',
    'os',
    'parse_filename_for_version',
    'process_media_selection',
    'process_torrent_selection',
    're',
    'record_torrent_addition',
    'remove_uncached_item',
    'render_template',
    'request',
    'requests',
    'run_scrape',
    'save_config',
    'scrape',
    'scraper_bp',
    'scraper_permission_required',
    'scraper_tester',
    'scraper_view_access_required',
    'search_analytics',
    'search_trakt',
    'select_episode',
    'select_media',
    'select_season',
    'session',
    'set_in_cache',
    'shows_trending',
    'tempfile',
    'threading',
    'time',
    'timezone',
    'tmdb_image_proxy',
    'trending_anime',
    'trending_movies',
    'trending_shows',
    'update_torrent_tracking',
    'user_required',
    'web_scrape_tvshow',
]
