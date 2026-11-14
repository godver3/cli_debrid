"""Every scheduled task in one table: interval, group, kind and when it is on by default.

ProgramRunner builds its schedule from this table, and the Automation page
(templates/automation.html) groups and labels tasks with it. Help text lives in
static/js/task_help_data.json.

Toggles saved on the Automation page are stored in task_toggles.json as overrides:
only tasks the user switched away from their default are written. A task with no
override follows its default, so turning on a setting (overlays, Plex mode...) turns
its tasks on even after the user has used the Automation page.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

TOGGLES_VERSION_KEY = '_migration_version'
# 0.6.34: every task's full on/off state. 2: overrides only.
LEGACY_TOGGLES_VERSION = '0.6.34'
TOGGLES_VERSION = '2'

# Kinds
QUEUE = 'queue'        # queue processing; always listed
FEATURE = 'feature'    # optional work a user may want on or off
INTERNAL = 'internal'  # housekeeping the app needs; listed under "Show internal"
MANUAL = 'manual'      # never scheduled; "Run now" only

# Groups, in display order
GROUPS = (
    ('queues', 'Queues', 'Moves items through Wanted, Scraping, Adding and Checking.'),
    ('content_sources', 'Content Sources', 'Fetches your watchlists, lists and requests.'),
    ('media_server', 'Plex & Media Server', 'Keeps your media server and the library in step.'),
    ('usenet', 'Usenet & Mount', 'Watches usenet downloads, repairs broken files and syncs with cli_mount.'),
    ('metadata', 'Metadata', 'Keeps release dates, titles and IDs up to date.'),
    ('library', 'Library & Upgrades', 'Backups, cleanups, upgrades and media analysis.'),
    ('system', 'System', 'Notifications, health checks and housekeeping.'),
    ('one_time', 'One-time fixes', 'Repairs you run once by hand.'),
)

HOUR = 3600
DAY = 24 * HOUR


@dataclass(frozen=True)
class TaskSpec:
    name: str
    label: str
    group: str
    interval: int = 0                 # seconds; 0 for manual tasks
    kind: str = FEATURE
    default: object = True            # bool, or callable(get_setting) -> bool
    controlled_by: str = ''           # settings location shown on the Automation page
    setting: Optional[Tuple[str, str]] = None  # on/off is this setting alone (toggle writes it)
    destructive: bool = False
    executor: str = 'default'         # APScheduler pool: 'queue' or 'default'

    def default_enabled(self, get_setting) -> bool:
        if self.kind == MANUAL:
            return False
        if self.setting:
            return bool(get_setting(*self.setting, False))
        if callable(self.default):
            try:
                return bool(self.default(get_setting))
            except Exception as e:
                logging.error(f"[TaskRegistry] default rule for {self.name} failed: {e}")
                return False
        return bool(self.default)


# ---- default rules -------------------------------------------------------------

def _fm(get):
    return get('File Management', 'file_collection_management', 'Symlinked/Local')


def _plex_checks_off_and_no_mount(get):
    """Plex mode with library checks disabled and no mounted path: nothing to check."""
    return (_fm(get) == 'Plex'
            and get('Plex', 'disable_plex_library_checks', False)
            and not str(get('Plex', 'mounted_file_location', '') or '').strip())


def _checking_default(get):
    return not _plex_checks_off_and_no_mount(get)


def _plex_full_scan_default(get):
    return _fm(get) != 'Symlinked/Local' and not _plex_checks_off_and_no_mount(get)


def _check_plex_files_default(get):
    if _fm(get) != 'Plex':
        return False
    return bool(get('Plex', 'update_plex_on_file_discovery', False)
                or get('Plex', 'disable_plex_library_checks', False))


def _jellyfin_configured(get):
    return bool(str(get('Debug', 'emby_jellyfin_url', '') or '').strip()
                and str(get('Debug', 'emby_jellyfin_token', '') or '').strip())


def _verify_symlinks_default(get):
    if _fm(get) != 'Symlinked/Local':
        return False
    plex = get('File Management', 'plex_url_for_symlink', '') and get('File Management', 'plex_token_for_symlink', '')
    return bool(plex or _jellyfin_configured(get))


def _external_adds_default(get):
    return _fm(get) == 'Symlinked/Local' and bool(get('File Management', 'scan_mount_for_external_adds', False))


def _plex_mode(get):
    return not (_fm(get) == 'Symlinked/Local' and get('File Management', 'media_server_type', '') == 'jellyfin')


def _viewer_history_default(get):
    if not str(get('TMDB', 'api_key', '') or '').strip():
        return False
    plex = get('Plex', 'url', '') and get('Plex', 'token', '')
    return bool(plex or _jellyfin_configured(get))


def _limited_environment(get):
    from utilities.set_supervisor_env import is_limited_environment
    return is_limited_environment()


def _overlays(get):
    return bool(get('Overlay Settings', 'overlays_enabled', False))


def _watch_history_default(get):
    return bool(get('Debug', 'do_not_add_plex_watch_history_items_to_queue', False))


# ---- the table -----------------------------------------------------------------

def _q(name, label, interval, default=True, controlled_by=''):
    return TaskSpec(name, label, 'queues', interval, QUEUE, default, controlled_by, executor='queue')


_SPECS = [
    # Queues
    _q('Wanted', 'Wanted', 60),
    _q('Scraping', 'Scraping', 1),
    _q('Adding', 'Adding', 1),
    _q('Checking', 'Checking', 30, _checking_default,
       'Off by default in Plex mode when Plex library checks are disabled and no mounted path is set'),
    _q('Sleeping', 'Sleeping', 300),
    _q('Unreleased', 'Unreleased', 300),
    _q('Blacklisted', 'Blacklisted', 2 * HOUR),
    _q('Pending Uncached', 'Pending Uncached', HOUR),
    _q('Upgrading', 'Upgrading', HOUR),
    _q('final_check_queue', 'Final Scrape', 900),
    _q('Pre_release', 'Pre-release', DAY),

    # Plex & media server
    TaskSpec('task_check_plex_files', 'Check Plex for new files', 'media_server', 60, default=_check_plex_files_default,
             controlled_by='Settings → Required → Plex (Update Plex on file discovery / Disable Plex library checks)',
             executor='queue'),
    TaskSpec('task_plex_full_scan', 'Plex full library scan', 'media_server', 1800, default=_plex_full_scan_default,
             controlled_by='On in Plex file management mode'),
    TaskSpec('task_manual_plex_full_scan', 'Plex full scan (extra)', 'media_server', HOUR, default=False),
    TaskSpec('task_verify_plex_removals', 'Detect items removed from the media server', 'media_server', 900),
    TaskSpec('task_process_standalone_plex_removals', 'Process media server removals', 'media_server', HOUR),
    TaskSpec('task_verify_symlinked_files', 'Verify symlinks are in the media server', 'media_server', 2 * HOUR,
             default=_verify_symlinks_default,
             controlled_by='On in Symlinked/Local mode when a media server is configured'),
    TaskSpec('task_scan_mount_for_external_adds', 'Pick up files added outside cli_debrid', 'media_server', 900,
             default=_external_adds_default,
             controlled_by='Settings → Required → File Management (Scan mount for external adds)'),
    TaskSpec('task_sync_library_metadata', 'Sync library metadata', 'media_server', DAY),
    TaskSpec('task_get_plex_watch_history', 'Import Plex watch history', 'media_server', DAY,
             default=_watch_history_default),
    TaskSpec('task_sync_viewer_history', 'Viewer history for recommendations', 'media_server', 6 * HOUR,
             default=_viewer_history_default, controlled_by='On when a media server and a TMDB key are configured'),
    TaskSpec('task_refresh_plex_tokens', 'Refresh Plex tokens', 'media_server', DAY),
    TaskSpec('task_sync_plex_labels', 'Sync Plex labels', 'media_server', 30 * 60, default=False),
    TaskSpec('task_process_plex_reported_issues', 'Replace items reported in Plex', 'media_server', 30,
             setting=('Plex Reported Issues', 'enabled'),
             controlled_by='Settings → Additional → Plex Reported Issues'),
    TaskSpec('task_plex_smart_collection_posters', 'Smart collection posters', 'media_server', DAY, default=_plex_mode),
    TaskSpec('task_plex_movie_boxsets', 'Movie box sets', 'media_server', DAY, default=_plex_mode),
    TaskSpec('task_overlay_sync', 'Poster overlays', 'media_server', HOUR, default=_overlays,
             controlled_by='Overlays page (Enable overlays)'),
    TaskSpec('task_overlay_cleanup', 'Poster overlay cleanup', 'media_server', DAY, default=_overlays,
             controlled_by='Overlays page (Enable overlays)'),

    # Usenet & mount
    TaskSpec('task_nzb_health_check', 'Watch usenet downloads', 'usenet', 10, executor='queue'),
    TaskSpec('task_nzb_playback_repair_completion', 'Finish playback repairs', 'usenet', 15),
    TaskSpec('task_nzb_playback_cleanup_retry', 'Retry old-file cleanup after repairs', 'usenet', 20 * 60),
    # Broken usenet files are repaired within minutes by task_nzb_health_check (it polls the
    # provider's broken list every 5 min) and the playback-repair path; this is the monthly
    # full re-check that catches old files whose articles have since expired.
    TaskSpec('task_repair_broken_nzbs', 'Scheduled repair scan', 'usenet', 30 * DAY),
    TaskSpec('task_repair_broken_debrids', 'Repair broken debrid files', 'usenet', 6 * HOUR),
    TaskSpec('task_sync_cli_mount_changes', 'Sync changes from cli_mount', 'usenet', 300),
    TaskSpec('task_push_pending_climount_tags', 'Push tags to cli_mount', 'usenet', 300),

    # Metadata
    TaskSpec('task_refresh_release_dates', 'Refresh release dates', 'metadata', 36600),
    TaskSpec('task_check_trakt_early_releases', 'Check Trakt early releases', 'metadata', 2 * HOUR),
    TaskSpec('task_precompute_airing_shows', 'Airing shows for the calendar', 'metadata', 600),
    TaskSpec('task_sync_episode_metadata', 'Sync episode titles', 'metadata', DAY, default=False),
    TaskSpec('task_cleanup_title_year_suffixes', 'Clean year suffixes from titles', 'metadata', DAY, default=False),
    TaskSpec('task_update_show_ids', 'Update show IDs', 'metadata', 40600, default=False),
    TaskSpec('task_update_show_titles', 'Update show titles', 'metadata', 45600, default=False),
    TaskSpec('task_update_movie_ids', 'Update movie IDs', 'metadata', 50600, default=False),
    TaskSpec('task_update_movie_titles', 'Update movie titles', 'metadata', 55600, default=False),
    TaskSpec('task_update_tv_show_status', 'Update show status (ended/airing)', 'metadata', 172800, default=False),

    # Library & upgrades
    TaskSpec('task_backup_database', 'Back up the database', 'library', DAY),
    TaskSpec('task_refresh_library_size_cache', 'Library size', 'library', 12 * HOUR),
    TaskSpec('task_run_library_maintenance', 'Library maintenance', 'library', 12 * HOUR, default=False,
             destructive=True),
    TaskSpec('task_analyze_media_files', 'Analyze media files', 'library', HOUR, default=False),
    TaskSpec('task_process_bulk_subtitles', 'Bulk subtitles', 'library', HOUR, default=False),
    TaskSpec('task_backup_debrid', 'Back up debrid account', 'library', DAY, default=False),
    TaskSpec('task_cleanup_debrid', 'Clean up debrid account', 'library', DAY, default=False, destructive=True),
    TaskSpec('task_upgrade_hub_scan', 'Upgrade Hub scan', 'library', DAY, default=False),
    TaskSpec('task_upgrade_hub_auto_queue', 'Upgrade Hub auto-queue', 'library', DAY, default=False),

    # System
    TaskSpec('task_send_notifications', 'Send notifications', 'system', 15, INTERNAL, executor='queue'),
    TaskSpec('task_update_queue_views', 'Refresh queue views', 'system', 30, INTERNAL, executor='queue'),
    TaskSpec('task_check_service_connectivity', 'Check service connectivity', 'system', 60, INTERNAL),
    TaskSpec('task_heartbeat', 'Heartbeat and queue watchdog', 'system', 120, INTERNAL),
    TaskSpec('task_refresh_download_stats', 'Download stats', 'system', 300, INTERNAL),
    TaskSpec('task_retry_content_source_removals', 'Retry failed watchlist removals', 'system', 300, INTERNAL),
    TaskSpec('task_reconcile_queues', 'Reconcile queues', 'system', HOUR, INTERNAL),
    TaskSpec('task_check_database_health', 'Database health check', 'system', HOUR, INTERNAL),
    TaskSpec('task_sync_time', 'Sync clock', 'system', HOUR, INTERNAL),
    TaskSpec('task_trim_memory', 'Return unused memory', 'system', HOUR, INTERNAL),
    TaskSpec('task_regulate_system_load', 'Slow down under high system load', 'system', 30,
             default=_limited_environment, executor='queue'),

    # One-time fixes (were daily tasks, off by default)
    TaskSpec('task_backfill_nzb_torrent_ids', 'Backfill NZB IDs', 'one_time', DAY, default=False),
    TaskSpec('task_backfill_plex_guids', 'Backfill Plex GUIDs', 'one_time', DAY, default=False),
    TaskSpec('task_backfill_plex_ms_item_id', 'Backfill Plex item IDs', 'one_time', DAY, default=False),

    # Manual only
    TaskSpec('task_purge_not_wanted_magnets_file', 'Purge not-wanted magnets', 'one_time', kind=MANUAL, destructive=True),
    TaskSpec('task_purge_not_wanted_urls_file', 'Purge not-wanted URLs', 'one_time', kind=MANUAL, destructive=True),
    TaskSpec('task_purge_not_wanted_nzbs_files', 'Purge not-wanted NZBs', 'one_time', kind=MANUAL, destructive=True),
    TaskSpec('task_purge_all_not_wanted_files', 'Purge all not-wanted lists', 'one_time', kind=MANUAL, destructive=True),
    TaskSpec('task_reset_trakt_auth', 'Reset Trakt sign-in', 'one_time', kind=MANUAL, destructive=True),
    TaskSpec('task_regenerate_labels_full', 'Rebuild Plex labels (full)', 'one_time', kind=MANUAL),
    TaskSpec('task_regenerate_labels_incremental', 'Rebuild Plex labels (recent)', 'one_time', kind=MANUAL),
    TaskSpec('task_regenerate_labels_from_backfilled_details', 'Rebuild Plex labels from details', 'one_time', kind=MANUAL),
    TaskSpec('task_backfill_missing_labels', 'Backfill missing Plex labels', 'one_time', kind=MANUAL),
    TaskSpec('task_backfill_plex_labels_content_source_detail', 'Backfill Plex label details', 'one_time', kind=MANUAL),
    TaskSpec('task_debug_log', 'Write a debug log line', 'one_time', kind=MANUAL),
]

SPECS: Dict[str, TaskSpec] = {s.name: s for s in _SPECS}
QUEUE_TASKS = tuple(s.name for s in _SPECS if s.kind == QUEUE)
SCHEDULED_SPECS = tuple(s for s in _SPECS if s.kind != MANUAL)

# Content source task names are task_<source id>_wanted.
CONTENT_SOURCE_INTERVALS = {
    'Collected': DAY,
}
CONTENT_SOURCE_DEFAULT_INTERVAL = 900


def content_source_task_name(source_id: str) -> str:
    return f'task_{source_id}_wanted'


def is_content_source_task(name: str) -> bool:
    return name.startswith('task_') and name.endswith('_wanted')


def content_source_default_interval(source_id: str, source_data=None) -> int:
    if isinstance(source_data, dict) and source_data.get('interval'):
        try:
            return int(source_data['interval'])
        except (TypeError, ValueError):
            pass
    return CONTENT_SOURCE_INTERVALS.get(source_id.split('_')[0], CONTENT_SOURCE_DEFAULT_INTERVAL)


def spec_for(name: str, content_sources=None) -> Optional[TaskSpec]:
    spec = SPECS.get(name)
    if spec:
        return spec
    if is_content_source_task(name):
        source_id = name[5:-7]
        data = (content_sources or {}).get(source_id)
        label = source_id
        if isinstance(data, dict) and str(data.get('display_name') or '').strip():
            label = data['display_name'].strip()
        return TaskSpec(name, label, 'content_sources',
                        content_source_default_interval(source_id, data))
    return None


def executor_for(name: str) -> str:
    spec = SPECS.get(name)
    return spec.executor if spec else 'default'


def default_intervals(content_sources=None) -> Dict[str, int]:
    out = {s.name: s.interval for s in SCHEDULED_SPECS}
    for source_id, data in (content_sources or {}).items():
        out[content_source_task_name(source_id)] = content_source_default_interval(source_id, data)
    return out


def default_enabled(get_setting, content_sources=None) -> Set[str]:
    on = {s.name for s in SCHEDULED_SPECS if s.default_enabled(get_setting)}
    # Content sources are on by default; the Automation page switches them off.
    for source_id in (content_sources or {}):
        on.add(content_source_task_name(source_id))
    return on


def apply_overrides(defaults: Set[str], overrides: Dict[str, bool], known: Iterable[str]) -> Set[str]:
    """defaults with the user's overrides applied. Setting-backed tasks ignore overrides."""
    known = set(known)
    enabled = set(defaults)
    for name, on in overrides.items():
        if name not in known or (SPECS.get(name) and SPECS[name].setting):
            continue
        if on:
            enabled.add(name)
        else:
            enabled.discard(name)
    return enabled


# ---- task_toggles.json ----------------------------------------------------------

def toggles_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'task_toggles.json')


def _read_toggles_file() -> dict:
    try:
        with open(toggles_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_toggles_file(overrides: Dict[str, bool]):
    data = dict(sorted(overrides.items()))
    data[TOGGLES_VERSION_KEY] = TOGGLES_VERSION
    path = toggles_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)


def load_overrides() -> Dict[str, bool]:
    data = _read_toggles_file()
    if data.get(TOGGLES_VERSION_KEY) != TOGGLES_VERSION:
        # Not converted yet (old full-state file): nothing in it is a trusted override.
        return {}
    return {k: bool(v) for k, v in data.items() if not k.startswith('_')}


def save_overrides(enabled: Set[str], defaults: Set[str], known: Iterable[str]):
    """Write only the tasks whose state differs from their default."""
    overrides = {}
    for name in known:
        spec = SPECS.get(name)
        if spec and (spec.setting or spec.kind == MANUAL):
            continue
        on = name in enabled
        if on != (name in defaults):
            overrides[name] = on
    _write_toggles_file(overrides)
    return overrides


def convert_legacy_toggles(get_setting, content_sources=None) -> bool:
    """Turn a 0.6.34 full-state task_toggles.json into overrides.

    Queue entries are dropped: they were never applied before (startup skipped any
    key without the task_ prefix), so honouring a stale "Checking: false" now would
    switch a queue off that has been running all along.
    """
    data = _read_toggles_file()
    if data.get(TOGGLES_VERSION_KEY) != LEGACY_TOGGLES_VERSION:
        return False
    defaults = default_enabled(get_setting, content_sources)
    known = set(default_intervals(content_sources))
    overrides = {}
    for name, on in data.items():
        if name.startswith('_') or name in QUEUE_TASKS or name not in known:
            continue
        spec = SPECS.get(name)
        if spec and spec.setting:
            continue
        if bool(on) != (name in defaults):
            overrides[name] = bool(on)
    _write_toggles_file(overrides)
    logging.info(f"[TaskRegistry] Converted task_toggles.json to overrides: {sorted(overrides)}")
    return True


def _drop_setting(section, key):
    from utilities.settings import load_config, save_config
    config = load_config()
    if key in (config.get(section) or {}):
        config[section].pop(key, None)
        save_config(config)


def migrate(get_setting, get_all_settings) -> None:
    """One-time moves of task settings into the Automation page. Safe to run every start."""
    data = _read_toggles_file()
    version = data.get(TOGGLES_VERSION_KEY)
    settings = get_all_settings() or {}
    content_sources = settings.get('Content Sources', {}) or {}
    if version == LEGACY_TOGGLES_VERSION:
        convert_legacy_toggles(get_setting, content_sources)
    elif version != TOGGLES_VERSION:
        _write_toggles_file({})

    debug = settings.get('Debug', {}) or {}

    # Debug.enable_library_maintenance_task -> an Automation toggle
    if 'enable_library_maintenance_task' in debug:
        if debug.get('enable_library_maintenance_task'):
            overrides = load_overrides()
            overrides.setdefault('task_run_library_maintenance', True)
            _write_toggles_file(overrides)
        _drop_setting('Debug', 'enable_library_maintenance_task')

    # Debug.content_source_check_period (minutes) -> task_intervals.json (seconds)
    periods = debug.get('content_source_check_period')
    if isinstance(periods, dict) and periods:
        path = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'task_intervals.json')
        try:
            with open(path) as f:
                intervals = json.load(f)
            if not isinstance(intervals, dict):
                intervals = {}
        except (OSError, ValueError):
            intervals = {}
        changed = False
        for source_id, minutes in periods.items():
            name = content_source_task_name(source_id)
            if intervals.get(name) is not None:
                continue
            try:
                minutes = float(minutes)
            except (TypeError, ValueError):
                continue
            # 15 is what the hidden settings field posted for every enabled source on
            # each save, not a value anyone chose; skip it so Collected keeps its 24h.
            if minutes == 15:
                continue
            seconds = int(minutes * 60)
            if seconds >= 10:
                intervals[name] = seconds
                changed = True
        if changed:
            with open(path, 'w') as f:
                json.dump(intervals, f, indent=4)
        _drop_setting('Debug', 'content_source_check_period')


def groups_payload():
    return [{'id': gid, 'label': label, 'description': desc} for gid, label, desc in GROUPS]
