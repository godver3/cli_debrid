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

"Nothing in the database points at it" is necessary but not sufficient evidence
that a folder came from outside. cli_debrid leaves its own untracked folders in
the mount too: cache-check probes add candidate magnets to the account, and a
grab that later fails has its item sent back to Wanted, which stops the DB
pointing at the torrent while the folder stays. Those are junk, not external
adds, and importing them reinstates releases the queues deliberately rejected --
so candidates are also checked against the not-wanted and blacklist sets
(_build_rejected_sets) before being imported.
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


def _build_rejected_sets() -> Set[str]:
    """Normalized mount-folder names cli_debrid added itself and then rejected.

    The premise of this whole task is that an untracked mount folder came from
    outside cli_debrid. That is not true of its own debris: every cache-check
    probe and every grab that later failed leaves a folder behind, and the DB
    stops pointing at it the moment the item is sent back to Wanted. Those
    folders then look exactly like an external add and get imported as Collected
    -- including the releases the queues had just blacklisted, which is how one
    episode ends up in the library a dozen times over.

    Two sources, both cheap:
      * torrent_additions rows whose hash is in the not-wanted set. item_data
        carries debrid_folder_name/filled_by_title, which is the mount folder
        name, so the hash-keyed not-wanted list can be matched against folders.
      * titles on media_items rows that are Blacklisted or ghostlisted.
    """
    from database.database_reading import normalize_string_for_comparison

    rejected: Set[str] = set()

    try:
        from database.not_wanted_magnets import get_not_wanted_magnets
        from database.not_wanted_magnets import get_base_filename
        # Entries are stored as bare hashes but magnets appear too; get_base_filename
        # normalizes both. Lowercased to match the hash column comparison below.
        not_wanted = set()
        for nw in (get_not_wanted_magnets() or []):
            if not nw:
                continue
            base = get_base_filename(nw)
            if base:
                not_wanted.add(base.lower())
    except Exception as e:
        logging.warning(f"[ExternalScan] Could not load not-wanted magnets: {e}")
        not_wanted = set()

    conn = None
    try:
        from database.core import get_db_connection
        conn = get_db_connection()

        if not_wanted:
            # Filtered in Python rather than a huge IN (...) -- the not-wanted set
            # runs to thousands of hashes and would blow SQLite's parameter limit.
            try:
                rows = conn.execute(
                    'SELECT torrent_hash, item_data FROM torrent_additions'
                ).fetchall()
            except Exception:
                rows = []  # table absent on a fresh install
            for row in rows:
                torrent_hash = (row['torrent_hash'] or '').lower()
                if torrent_hash not in not_wanted:
                    continue
                try:
                    data = json.loads(row['item_data']) if row['item_data'] else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                for key in ('debrid_folder_name', 'filled_by_title'):
                    value = data.get(key)
                    if value:
                        rejected.add(normalize_string_for_comparison(value))

        rows = conn.execute(
            "SELECT filled_by_title, real_debrid_original_title, "
            "original_scraped_torrent_title FROM media_items "
            "WHERE state = 'Blacklisted' OR ghostlisted = 1"
        ).fetchall()
        for row in rows:
            for value in (row['filled_by_title'], row['real_debrid_original_title'],
                          row['original_scraped_torrent_title']):
                if value:
                    rejected.add(normalize_string_for_comparison(value))
    except Exception as e:
        logging.error(f"[ExternalScan] Could not build rejected-content lookups: {e}", exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    rejected.discard('')
    return rejected


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


def _is_rejected(folder_name: str, rejected_names: Set[str]) -> bool:
    """True when this mount folder is a release cli_debrid itself rejected.

    Kept beside _already_tracked, and lazily importing the same normalizer, so
    both membership tests treat folder names identically.
    """
    if not rejected_names:
        return False
    from database.database_reading import normalize_string_for_comparison
    return normalize_string_for_comparison(folder_name) in rejected_names


def _should_retry(entry: Dict, now: float) -> bool:
    # Present in the mount before the feature was turned on. Never auto-import
    # these -- the whole point of the baseline is that enabling the task does
    # not drag an established library's worth of untracked content into the DB.
    if entry.get('baseline'):
        return False
    if entry.get('imported'):
        return False
    if entry.get('rejected'):
        # Known junk, but re-check on the normal window rather than permanently:
        # the user may since have un-blacklisted it and gone and fetched it by
        # hand, which is precisely the case this task exists to catch. Does not
        # consume an 'attempts' slot -- no import was tried.
        return (now - entry.get('last_attempt', 0)) >= RETRY_AFTER_SECONDS
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
        'rejected': 0,
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

    # Only write the state file when something actually changed. The steady
    # state is a large mount where every entry is already known, and rewriting
    # a file with one key per mount folder on every run buys nothing.
    dirty = False

    # Drop state for folders that have gone from the mount, so the file
    # doesn't grow without bound.
    present = set(entries)
    for name in [n for n in state if n not in present]:
        del state[name]
        dirty = True

    candidates = []
    for name in entries:
        entry = state.get(name)
        if entry is not None and not _should_retry(entry, now):
            continue
        candidates.append(name)

    if not candidates:
        if dirty:
            _save_state(state)
        return summary

    known_titles, known_components = _build_known_sets()
    rejected_names = _build_rejected_sets()

    new_folders = []
    for name in candidates:
        if _already_tracked(name, known_titles, known_components):
            # Tracked by cli_debrid already - record it so we don't re-check
            # it against the DB on every run.
            state[name] = {'imported': True, 'tracked_existing': True, 'first_seen': now}
            dirty = True
            continue
        if _is_rejected(name, rejected_names):
            # cli_debrid's own leftover, not an external add: this release was
            # blacklisted or ghostlisted, so importing it would reinstate exactly
            # what the queues rejected.
            entry = state.get(name) or {'first_seen': now}
            entry['rejected'] = True
            entry['last_attempt'] = now
            state[name] = entry
            summary['rejected'] += 1
            dirty = True
            logging.info(f"[ExternalScan] Skipping '{name}' - blacklisted/not-wanted content "
                         f"cli_debrid rejected itself, not an external add.")
            continue
        new_folders.append(name)

    summary['new_candidates'] = len(new_folders)
    if not new_folders:
        if dirty:
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
            #
            # user_initiated stays False. The webhook passes True because a person
            # deliberately put that content in the debrid account, which justifies
            # unghosting an existing entry. This task runs on a timer over whatever
            # happens to be in the mount, including cli_debrid's own leftovers, so
            # the ghostlist/blacklist guard is the only thing stopping it importing
            # releases the queues just rejected.
            _run_rclone_to_symlink_task(
                scan_path,
                symlink_base_path,
                False,               # dry_run
                task_id,
                True,                # trigger_plex_update_on_success
                name,                # assumed_item_title_from_path
                False,               # user_initiated - see below
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
