"""routes.magnet_routes (split into a package)."""

# Split from one large module; the old import path still resolves every name.
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
import threading as _threading
from debrid import get_debrid_provider
from database.database_writing import add_media_item, replace_collected_item
from ..models import admin_required
from queues.config_manager import load_config
from datetime import datetime, timezone
from queues.media_matcher import MediaMatcher
import logging
from usenet import get_usenet_provider_display_name as _usenet_pname
from cli_battery.app.direct_api import DirectAPI
from cli_battery.app import trakt_client
import os
import re
from database.torrent_tracking import update_torrent_tracking
from content_checkers.content_source_detail import append_content_source_detail
from scraper.functions.ptt_parser import parse_with_ptt
import requests
from content_checkers.trakt import REQUEST_TIMEOUT, TRAKT_API_URL, get_trakt_headers
import json
from fuzzywuzzy import fuzz
import asyncio
from utilities.web_scraper import SearchCache, get_media_meta
from utilities.settings import get_setting
from typing import Any, Dict, List, Optional
from ._common import (
    _assignment_store,
    _assignment_store_lock,
    _fetch_media_details_for_assigner,
    _fetch_trakt_season_data_directly,
    _library_episode_rows,
    _mark_items_in_library,
    _order_change_blocked,
    _requested_episode_order,
    _save_episode_order,
    _saved_episode_order,
    _season_cache,
    _seasons_for_order,
    _sync_plex_order_for_new_show,
    get_versions,
    magnet_bp,
)
from .season_data import (
    episode_orders,
    get_season_data,
)
from .assign import (
    assign_magnet,
)
from .item_create import (
    create_episode_item,
    create_full_series_items,
    create_movie_item,
    create_season_items,
    verify_media_type,
)
from .assignment_build import (
    _AssignmentError,
    _build_assignment,
    _episode_list,
    _store_assignment,
    _video_files,
)
from .manual_assign import (
    _assignment_page_data,
    fill_gaps,
    prepare_manual_assignment,
    rematch_manual_assignment,
    show_manual_assignment,
)
from .confirm import (
    _add_empty_to_wanted,
    confirm_manual_assignment,
)

__all__ = [
    'Any',
    'Blueprint',
    'Dict',
    'DirectAPI',
    'List',
    'MediaMatcher',
    'Optional',
    'REQUEST_TIMEOUT',
    'SearchCache',
    'TRAKT_API_URL',
    '_AssignmentError',
    '_add_empty_to_wanted',
    '_assignment_page_data',
    '_assignment_store',
    '_assignment_store_lock',
    '_build_assignment',
    '_episode_list',
    '_fetch_media_details_for_assigner',
    '_fetch_trakt_season_data_directly',
    '_library_episode_rows',
    '_mark_items_in_library',
    '_order_change_blocked',
    '_requested_episode_order',
    '_save_episode_order',
    '_saved_episode_order',
    '_season_cache',
    '_seasons_for_order',
    '_store_assignment',
    '_sync_plex_order_for_new_show',
    '_threading',
    '_usenet_pname',
    '_video_files',
    'add_media_item',
    'admin_required',
    'append_content_source_detail',
    'assign_magnet',
    'asyncio',
    'confirm_manual_assignment',
    'create_episode_item',
    'create_full_series_items',
    'create_movie_item',
    'create_season_items',
    'datetime',
    'episode_orders',
    'fill_gaps',
    'flash',
    'fuzz',
    'get_debrid_provider',
    'get_media_meta',
    'get_season_data',
    'get_setting',
    'get_trakt_headers',
    'get_versions',
    'json',
    'jsonify',
    'load_config',
    'logging',
    'magnet_bp',
    'os',
    'parse_with_ptt',
    'prepare_manual_assignment',
    're',
    'redirect',
    'rematch_manual_assignment',
    'render_template',
    'replace_collected_item',
    'request',
    'requests',
    'show_manual_assignment',
    'timezone',
    'trakt_client',
    'update_torrent_tracking',
    'url_for',
    'verify_media_type',
]
