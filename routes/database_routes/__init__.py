"""routes.database_routes (split into a package)."""

# Split from one large module; the old import path still resolves every name.
from flask import Blueprint, current_app, flash, jsonify, render_template, request, session
import sqlite3
import string
import logging
from sqlalchemy import inspect, text
from routes.extensions import db
from utilities.settings import get_setting
import json
from utilities.reverse_parser import get_default_version, get_version_order, get_version_settings, parse_filename_for_version
from ..models import admin_required, user_required
from utilities.plex_removal_cache import cache_plex_removal
from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
from database.database_reading import get_media_item_by_id
import os
from datetime import datetime
from time import sleep
import time
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from utilities.web_scraper import get_media_meta
from queues.config_manager import get_content_source_display_names
from utilities.local_library_scan import convert_item_to_symlink
from database.database_writing import RESET_COLLECTION_STATE_SQL, update_media_item
from database.symlink_verification import add_symlinked_file_for_verification
from ._common import (
    BATCH_SIZE,
    PER_PAGE,
    STATS_CACHE_DURATION_SECONDS,
    database_bp,
    get_basic_collection_counts,
    item_source_label,
    queue_plex_removal_for_item,
    source_filter_clause,
    source_sql,
    sync_queues_after_rescrape,
    translate_plex_path_to_local,
)
from .index import (
    cached_stats_data,
    index,
    stats_cache_timestamp,
)
from .item_actions import (
    _find_rescrape_candidates,
    delete_item,
    perform_database_migration,
    rescrape_item,
    rescrape_single_item,
)
from .bulk_actions import (
    bulk_queue_action,
)
from .tools import (
    apply_parsed_versions,
    clear_watch_history,
    get_column_values,
    phalanxdb_status,
    reverse_parser,
    test_phalanx_hash,
    visual_browser,
    visual_data,
    watch_history,
)

__all__ = [
    'BATCH_SIZE',
    'Blueprint',
    'PER_PAGE',
    'PhalanxDBClassManager',
    'RESET_COLLECTION_STATE_SQL',
    'STATS_CACHE_DURATION_SECONDS',
    '_find_rescrape_candidates',
    'add_symlinked_file_for_verification',
    'admin_required',
    'apply_parsed_versions',
    'bulk_queue_action',
    'cache_plex_removal',
    'cached_stats_data',
    'clear_watch_history',
    'convert_item_to_symlink',
    'current_app',
    'database_bp',
    'datetime',
    'db',
    'delete_item',
    'flash',
    'get_basic_collection_counts',
    'get_column_values',
    'get_content_source_display_names',
    'get_default_version',
    'get_media_item_by_id',
    'get_media_meta',
    'get_setting',
    'get_version_order',
    'get_version_settings',
    'index',
    'inspect',
    'item_source_label',
    'json',
    'jsonify',
    'logging',
    'os',
    'parse_filename_for_version',
    'perform_database_migration',
    'phalanxdb_status',
    'queue_plex_removal_for_item',
    'remove_file_from_plex',
    'render_template',
    'request',
    'rescrape_item',
    'rescrape_single_item',
    'reverse_parser',
    'scan_and_empty_plex_trash',
    'session',
    'sleep',
    'source_filter_clause',
    'source_sql',
    'sqlite3',
    'stats_cache_timestamp',
    'string',
    'sync_queues_after_rescrape',
    'test_phalanx_hash',
    'text',
    'time',
    'translate_plex_path_to_local',
    'update_media_item',
    'user_required',
    'visual_browser',
    'visual_data',
    'watch_history',
]
