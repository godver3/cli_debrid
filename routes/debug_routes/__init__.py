"""routes.debug_routes (split into a package)."""

# Split from one large module; the old import path still resolves every name.
from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, stream_with_context, url_for
from queues.run_program import ProgramRunner, get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex
from database.manual_blacklist import add_to_manual_blacklist, get_manual_blacklist, remove_from_manual_blacklist, save_manual_blacklist
from database.core import add_db_notification
from utilities.settings import get_all_settings, get_setting, set_setting
from queues.config_manager import load_config
import logging
from routes import admin_required
from routes.models import user_required
from cli_battery.app.direct_api import DirectAPI
from database.torrent_tracking import get_recent_additions, get_torrent_history
import os
import glob
from routes.api_tracker import api
import time
from datetime import datetime
from routes.notifications import get_enabled_notifications, send_notifications
from datetime import timedelta
from database.not_wanted_magnets import get_not_wanted_magnets, get_not_wanted_urls, save_not_wanted_magnets, save_not_wanted_urls
import json
from debrid import get_debrid_provider
from utilities.deletion_manager import DeletionManager
from routes.program_operation_routes import get_program_runner
import threading
import asyncio
from utilities.plex_functions import plex_update_item
from content_checkers.content_cache_management import UNRESOLVED_RETRY_HOURS, item_has_metadata_output, load_source_cache, metadata_output_ids, normalize_enabled_versions, save_source_cache, update_cache_for_item
from content_checkers.source_run_report import SourceRunReport
from content_checkers.blocked_items import drop_blocked_items
from database.symlink_verification import get_unverified_files, get_verification_stats
import re
from pathlib import Path
from utilities.reverse_parser import parse_filename_for_version
import sqlite3
from utilities.local_library_scan import convert_item_to_symlink, create_symlink, get_symlink_path, resync_symlinks_with_new_settings
from scraper.functions.ptt_parser import is_obfuscated_filename, parse_with_ptt
from database.database_writing import RESET_COLLECTION_STATE_SQL, add_media_item
from utilities.plex_removal_cache import cache_plex_removal
import subprocess
from ._common import (
    analysis_progress,
    debug_bp,
    rclone_scan_progress,
    scan_progress,
)
from .maintenance import (
    bulk_delete_by_imdb,
    debug_functions,
    delete_database,
    get_queue_contents,
    refresh_release_dates_route,
    reset_battery_show_cache,
)
from .blacklist import (
    manual_blacklist,
    manual_blacklist_seasons,
)
from .plex_scan import (
    async_get_collected_from_plex,
    direct_plex_scan,
    get_collected_from_plex,
    plex_scan_progress,
    task_status,
    update_trakt_settings,
)
from .wanted_content import (
    async_get_wanted_content,
    get_and_add_wanted_content,
    get_content_sources,
    get_rate_limit_info,
    get_wanted_content,
)
from .item_actions import (
    move_item_to_wanted,
    move_to_upgrading,
    rescrape_item,
    send_test_notification,
)
from .tasks import (
    get_available_tasks,
    run_full_climount_sync,
    run_task,
    sync_plex_labels,
)
from .not_wanted import (
    not_wanted,
    purge_not_wanted,
    remove_not_wanted_magnet,
    remove_not_wanted_url,
)
from .versions import (
    get_available_versions,
    get_versions,
    propagate_version,
)
from .symlink_convert import (
    conversion_progress,
    convert_to_symlinks,
)
from .verification import (
    delete_cache_files_route,
    direct_emby_scan,
    get_cache_files,
    get_trakt_token_status,
    get_verification_queue,
    torrent_tracking,
    validate_plex_tokens_route,
    verify_torrent,
)
from .symlink_recovery import (
    _run_analysis_thread,
    analysis_progress_stream,
    analyze_symlinks,
    modify_symlink_paths,
    parse_symlink,
    perform_recovery,
    recover_symlinks_page,
)
from .rclone_scan import (
    _describe_add_media_item_failure,
    _run_rclone_to_symlink_task,
    delete_battery_db_files,
    rclone_scan_progress_stream,
    rclone_to_symlinks_route,
    resync_symlinks_route,
)
from .duplicates import (
    async_run_bulk_subs,
    cleanup_collected_watchlist,
    fix_episode_numbers,
    remove_duplicate_items,
    rescrape_duplicate_symlinks,
    run_bulk_subtitle_scan,
    scan_duplicate_symlinks,
)
from .upgrade_cleanup import (
    cleanup_failed_upgrades,
    cleanup_progress,
    get_cleanup_progress,
)
from .db_backups import (
    delete_old_databases,
    list_database_backups,
    request_database_restore,
    scan_old_databases,
)
from .memory import (
    memory_snapshot,
    trim_memory,
)
from .climount import (
    _climount_cleanup_jobs,
    _dcy_age_display,
    _dcy_is_valid_hybr,
    _get_dcy_dirs,
    backfill_nzb_names,
    backfill_nzb_names_status,
    climount_backup_now,
    climount_cleanup,
    climount_cleanup_status,
    climount_providers,
    delete_climount_old_databases,
    list_climount_backups,
    request_climount_restore,
    scan_climount_old_databases,
)

__all__ = [
    'Blueprint',
    'DeletionManager',
    'DirectAPI',
    'Path',
    'ProgramRunner',
    'RESET_COLLECTION_STATE_SQL',
    'Response',
    'SourceRunReport',
    'UNRESOLVED_RETRY_HOURS',
    '_climount_cleanup_jobs',
    '_dcy_age_display',
    '_dcy_is_valid_hybr',
    '_describe_add_media_item_failure',
    '_get_dcy_dirs',
    '_run_analysis_thread',
    '_run_rclone_to_symlink_task',
    'add_db_notification',
    'add_media_item',
    'add_to_manual_blacklist',
    'admin_required',
    'analysis_progress',
    'analysis_progress_stream',
    'analyze_symlinks',
    'api',
    'async_get_collected_from_plex',
    'async_get_wanted_content',
    'async_run_bulk_subs',
    'asyncio',
    'backfill_nzb_names',
    'backfill_nzb_names_status',
    'bulk_delete_by_imdb',
    'cache_plex_removal',
    'cleanup_collected_watchlist',
    'cleanup_failed_upgrades',
    'cleanup_progress',
    'climount_backup_now',
    'climount_cleanup',
    'climount_cleanup_status',
    'climount_providers',
    'conversion_progress',
    'convert_item_to_symlink',
    'convert_to_symlinks',
    'create_symlink',
    'current_app',
    'datetime',
    'debug_bp',
    'debug_functions',
    'delete_battery_db_files',
    'delete_cache_files_route',
    'delete_climount_old_databases',
    'delete_database',
    'delete_old_databases',
    'direct_emby_scan',
    'direct_plex_scan',
    'drop_blocked_items',
    'fix_episode_numbers',
    'flash',
    'get_all_settings',
    'get_and_add_all_collected_from_plex',
    'get_and_add_recent_collected_from_plex',
    'get_and_add_wanted_content',
    'get_available_tasks',
    'get_available_versions',
    'get_cache_files',
    'get_cleanup_progress',
    'get_collected_from_plex',
    'get_content_sources',
    'get_debrid_provider',
    'get_enabled_notifications',
    'get_manual_blacklist',
    'get_not_wanted_magnets',
    'get_not_wanted_urls',
    'get_program_runner',
    'get_queue_contents',
    'get_rate_limit_info',
    'get_recent_additions',
    'get_setting',
    'get_symlink_path',
    'get_torrent_history',
    'get_trakt_token_status',
    'get_unverified_files',
    'get_verification_queue',
    'get_verification_stats',
    'get_versions',
    'get_wanted_content',
    'glob',
    'is_obfuscated_filename',
    'item_has_metadata_output',
    'json',
    'jsonify',
    'list_climount_backups',
    'list_database_backups',
    'load_config',
    'load_source_cache',
    'logging',
    'manual_blacklist',
    'manual_blacklist_seasons',
    'memory_snapshot',
    'metadata_output_ids',
    'modify_symlink_paths',
    'move_item_to_wanted',
    'move_to_upgrading',
    'normalize_enabled_versions',
    'not_wanted',
    'os',
    'parse_filename_for_version',
    'parse_symlink',
    'parse_with_ptt',
    'perform_recovery',
    'plex_scan_progress',
    'plex_update_item',
    'propagate_version',
    'purge_not_wanted',
    'rclone_scan_progress',
    'rclone_scan_progress_stream',
    'rclone_to_symlinks_route',
    're',
    'recover_symlinks_page',
    'redirect',
    'refresh_release_dates_route',
    'remove_duplicate_items',
    'remove_from_manual_blacklist',
    'remove_not_wanted_magnet',
    'remove_not_wanted_url',
    'render_template',
    'request',
    'request_climount_restore',
    'request_database_restore',
    'rescrape_duplicate_symlinks',
    'rescrape_item',
    'reset_battery_show_cache',
    'resync_symlinks_route',
    'resync_symlinks_with_new_settings',
    'run_bulk_subtitle_scan',
    'run_full_climount_sync',
    'run_task',
    'save_manual_blacklist',
    'save_not_wanted_magnets',
    'save_not_wanted_urls',
    'save_source_cache',
    'scan_climount_old_databases',
    'scan_duplicate_symlinks',
    'scan_old_databases',
    'scan_progress',
    'send_notifications',
    'send_test_notification',
    'set_setting',
    'sqlite3',
    'stream_with_context',
    'subprocess',
    'sync_plex_labels',
    'task_status',
    'threading',
    'time',
    'timedelta',
    'torrent_tracking',
    'trim_memory',
    'update_cache_for_item',
    'update_trakt_settings',
    'url_for',
    'user_required',
    'validate_plex_tokens_route',
    'verify_torrent',
]
