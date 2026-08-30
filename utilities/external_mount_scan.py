"""
Periodic scan of the debrid mount for content added outside cli_debrid.

Symlink mode only. cli_debrid normally learns about new files because it put
them there itself (Wanted -> Scraping -> Adding -> Checking). Content added
straight to the debrid account by something else -- DMM, the provider's own
web UI, a manual magnet -- shows up in the mount with nothing in the database
pointing at it, so no symlink is created and the media server is never told.

Zurg users get this handled by its on_library_update hook calling
/webhook/rclone. Decypharr and plain rclone mounts have no such hook, so this
task polls the mount instead and feeds anything new into the exact same
pipeline the webhook uses (_run_rclone_to_symlink_task).

First run records everything already in the mount as a baseline without
importing it -- otherwise enabling this on an established library would mass
import years of untracked content in one go. Delete the state file to
re-baseline.
"""

import json
import logging
import os
import time
import uuid
from typing import Dict, List, Set

from utilities.settings import get_setting

STATE_FILENAME = 'external_mount_scan_state.json'

# Cap per run so a mount full of untracked folders can't spend hours in one
# pass hammering the metadata APIs. The remainder is picked up next run.
MAX_IMPORTS_PER_RUN = 25

# A folder whose metadata lookup keeps failing shouldn't be retried forever.
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 6 * 60 * 60


def _state_path() -> str:
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    return os.path.join(db_content_dir, STATE_FILENAME)


def _load_state() -> Dict:
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"[ExternalScan] Could not read state file {path}: {e}. Treating as empty.")
        return {}


def _save_state(state: Dict) -> None:
    path = _state_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logging.error(f"[ExternalScan] Could not write state file {path}: {e}")


def _list_top_level(mount_path: str) -> List[str]:
    """Top-level directory names only -- one directory listing, no recursive
    walk. A recursive walk over a WebDAV-backed mount is far too expensive to
    run on a timer.

    Directories only: debrid mounts expose one folder per torrent (even
    single-file ones), and _run_rclone_to_symlink_task rejects a non-directory
    scan path outright.
    """
    try:
        with os.scandir(mount_path) as it:
            return [entry.name for entry in it
                    if not entry.name.startswith('.') and entry.is_dir()]
    except OSError as e:
        logging.error(f"[ExternalScan] Could not list mount path '{mount_path}': {e}")
        return []


def _build_known_sets() -> tuple:
    """Build in-memory lookups of what the DB already knows about.

    This is only a fast pre-filter for the common 'already tracked' case. The
    per-folder helpers the webhook uses do a full-table scan in Python each
    call, which is fine for one webhook but not for thousands of folders per
    run. Anything this filter does not positively identify still goes through
    those authoritative helpers, so the semantics match the webhook exactly.
    """
    from database.core import get_db_connection
    from database.database_reading import normalize_string_for_comparison

    known_titles: Set[str] = set()
    known_components: Set[str] = set()

    conn = None
    try:
        conn = get_db_connection()

        rows = conn.execute(
            'SELECT filled_by_title, real_debrid_original_title FROM media_items '
            'WHERE filled_by_title IS NOT NULL OR real_debrid_original_title IS NOT NULL'
        ).fetchall()
        for row in rows:
            for value in (row['filled_by_title'], row['real_debrid_original_title']):
                if value:
                    known_titles.add(normalize_string_for_comparison(value))

        rows = conn.execute(
            'SELECT original_path_for_symlink FROM media_items '
            'WHERE original_path_for_symlink IS NOT NULL'
        ).fetchall()
        for row in rows:
            path = row['original_path_for_symlink']
            if not path:
                continue
            for part in path.replace('\\', '/').split('/'):
                if part:
                    known_components.add(normalize_string_for_comparison(part))
    except Exception as e:
        logging.error(f"[ExternalScan] Could not build known-item lookups: {e}", exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return known_titles, known_components


def _already_tracked(folder_name: str, known_titles: Set[str], known_components: Set[str]) -> bool:
    """Fast pre-filter, then the same checks the rclone webhook makes."""
    from database.database_reading import (
        normalize_string_for_comparison,
        check_item_exists_by_directory_name,
        check_item_exists_with_symlink_path_containing,
    )

    normalized = normalize_string_for_comparison(folder_name)
    if normalized in known_titles or normalized in known_components:
        return True

    # Not positively identified above -- fall through to the authoritative
    # (expensive) checks. Only runs for the handful of genuinely new folders.
    if check_item_exists_by_directory_name(folder_name):
        return True
    if check_item_exists_with_symlink_path_containing(folder_name):
        return True
    return False


def _should_retry(entry: Dict, now: float) -> bool:
    # Present in the mount before the feature was turned on. Never auto-import
    # these -- the whole point of the baseline is that enabling the task does
    # not drag an established library's worth of untracked content into the DB.
    if entry.get('baseline'):
        return False
    if entry.get('imported'):
        return False
    if entry.get('attempts', 0) >= MAX_ATTEMPTS:
        return False
    last = entry.get('last_attempt', 0)
    return (now - last) >= RETRY_AFTER_SECONDS


def scan_mount_for_external_adds() -> Dict:
    """Scan the mount for folders cli_debrid has no record of and import them.

    Returns a summary dict for logging.
    """
    summary = {
        'skipped_reason': None,
        'entries_seen': 0,
        'new_candidates': 0,
        'imported': 0,
        'failed': 0,
        'baselined': 0,
    }

    if get_setting('File Management', 'file_collection_management', 'Symlinked/Local') != 'Symlinked/Local':
        summary['skipped_reason'] = 'file collection management is not Symlinked/Local'
        return summary

    mount_path = get_setting('File Management', 'original_files_path', '')
    if not mount_path or not os.path.isdir(mount_path):
        summary['skipped_reason'] = f"original_files_path '{mount_path}' is not a readable directory"
        return summary

    symlink_base_path = get_setting('File Management', 'symlinked_files_path', '')
    if not symlink_base_path:
        summary['skipped_reason'] = 'symlinked_files_path is not configured'
        return summary

    entries = _list_top_level(mount_path)
    summary['entries_seen'] = len(entries)
    if not entries:
        return summary

    state = _load_state()
    is_first_run = not state
    now = time.time()

    # First run: record what's already there and import none of it.
    if is_first_run:
        for name in entries:
            state[name] = {'imported': False, 'baseline': True, 'first_seen': now}
        _save_state(state)
        summary['baselined'] = len(entries)
        logging.info(
            f"[ExternalScan] First run - recorded {len(entries)} existing mount entries as baseline "
            f"without importing. Content appearing from now on will be imported. "
            f"Delete {_state_path()} to re-baseline."
        )
        return summary

    # Drop state for folders that have gone from the mount, so the file
    # doesn't grow without bound.
    present = set(entries)
    for name in [n for n in state if n not in present]:
        del state[name]

    candidates = []
    for name in entries:
        entry = state.get(name)
        if entry is not None and not _should_retry(entry, now):
            continue
        candidates.append(name)

    if not candidates:
        _save_state(state)
        return summary

    known_titles, known_components = _build_known_sets()

    new_folders = []
    for name in candidates:
        if _already_tracked(name, known_titles, known_components):
            # Tracked by cli_debrid already - record it so we don't re-check
            # it against the DB on every run.
            state[name] = {'imported': True, 'tracked_existing': True, 'first_seen': now}
            continue
        new_folders.append(name)

    summary['new_candidates'] = len(new_folders)
    if not new_folders:
        _save_state(state)
        return summary

    logging.info(f"[ExternalScan] {len(new_folders)} mount entries with no database record; "
                 f"importing up to {MAX_IMPORTS_PER_RUN} this run.")

    # Imported lazily: routes.debug_routes pulls in a large chunk of the app,
    # and importing it at module level from utilities risks a circular import.
    from routes.debug_routes import _run_rclone_to_symlink_task, rclone_scan_progress

    for name in new_folders[:MAX_IMPORTS_PER_RUN]:
        entry = state.get(name) or {'first_seen': now}
        entry['attempts'] = entry.get('attempts', 0) + 1
        entry['last_attempt'] = now

        scan_path = os.path.join(mount_path, name)
        task_id = str(uuid.uuid4())

        logging.info(f"[ExternalScan] Importing external add: {scan_path}")
        try:
            # Same call the rclone webhook makes - metadata lookup, DB insert
            # as Collected, symlink creation, media server scan and the
            # 'collected' notification all happen inside.
            _run_rclone_to_symlink_task(
                scan_path,
                symlink_base_path,
                False,               # dry_run
                task_id,
                True,                # trigger_plex_update_on_success
                name,                # assumed_item_title_from_path
            )
            progress = rclone_scan_progress.get(task_id) or {}
            added = progress.get('items_added_to_db', 0)
            symlinks = progress.get('symlinks_created', 0)
            if added > 0:
                entry['imported'] = True
                summary['imported'] += 1
                logging.info(f"[ExternalScan] Imported '{name}' - {added} item(s) added, "
                             f"{symlinks} symlink(s) created.")
            else:
                summary['failed'] += 1
                logging.warning(
                    f"[ExternalScan] '{name}' produced no database entries "
                    f"(attempt {entry['attempts']}/{MAX_ATTEMPTS}): "
                    f"{progress.get('message', 'no progress reported')}"
                )
        except Exception as e:
            summary['failed'] += 1
            logging.error(f"[ExternalScan] Import failed for '{name}': {e}", exc_info=True)

        state[name] = entry

    _save_state(state)
    return summary
