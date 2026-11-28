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

Safeguards, each there because a periodic import over a live mount goes wrong
in a specific way:
  * Own leftovers: a folder whose name matches ANY torrent_additions record
    (or a media_items title column) is cli_debrid's own, whatever its hash's
    not-wanted status. A trailing " (N)" duplicate suffix is ignored.
  * Settle time: a new folder is only recorded on first sight; it is imported
    once a later run sees it AND it is SETTLE_SECONDS old, so a torrent still
    being added is not imported half-populated.
  * No media yet: a run that found zero video files does not use up an attempt;
    it is retried every run until PENDING_GIVE_UP_SECONDS after first_seen.
  * Partial imports stay retryable (only an error-free run is 'imported'), and
    the pipeline is asked to skip files already in media_items so retries do not
    duplicate rows.
  * Baseline robustness: a '__meta__' key keeps baselined_at/last_run/
    last_entry_count. A corrupt state file is moved aside and the run skipped;
    a listing under half the previous size is treated as a flaky mount; a run
    after a >2 day gap baselines new folders instead of importing them.
  * Layouts: original_files_path (stepping into '__all__' if the mount root was
    configured) and, if set and separate, the Usenet Provider mount, including
    NzbDAV's <category>/<job> nesting. Usenet keys are prefixed 'usenet:'.
  * Runs never overlap (module lock) and the import pipeline refuses a path
    another scan is already processing.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

from utilities.settings import get_setting

STATE_FILENAME = 'external_mount_scan_state.json'

# Cap per run so a mount full of untracked folders can't spend hours in one
# pass hammering the metadata APIs. The remainder is picked up next run.
MAX_IMPORTS_PER_RUN = 25

# A folder whose metadata lookup keeps failing shouldn't be retried forever.
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 6 * 60 * 60

# A folder missing from one listing is kept in state this long before it's
# forgotten. A flaky mount can list a fraction of its folders; dropping their
# state at once turned every baseline folder into a "new" import when the
# listing recovered.
GONE_GRACE_SECONDS = 7 * 24 * 60 * 60

# A new folder must have been first seen this long ago (and on an earlier run)
# before it is imported, so a torrent still being populated isn't half-imported.
SETTLE_SECONDS = 10 * 60

# A folder with no media files yet is retried every run for this long.
PENDING_GIVE_UP_SECONDS = 24 * 60 * 60

# If the task was off longer than this, whatever appeared meanwhile is
# baselined rather than imported (it is not "new", it is just unseen).
LONG_GAP_SECONDS = 2 * 24 * 60 * 60

# Flaky-listing guard: a listing under half the previous size, when the previous
# one was at least this big, is a mount hiccup, not hundreds of deletions.
FLAKY_MIN_COUNT = 20
FLAKY_RATIO = 0.5
# ...unless it stays that small this many runs in a row (an hour at the 15 min
# interval): then the folders really went (account cleared), so accept the new
# count instead of skipping the root forever.
FLAKY_ACCEPT_AFTER = 4

META_KEY = '__meta__'
USENET_PREFIX = 'usenet:'
# last_run only needs to be fresh to within the long-gap window; don't rewrite
# the state file every run just to bump it.
LAST_RUN_WRITE_INTERVAL = 60 * 60

_SCAN_LOCK = threading.Lock()
_warned_all_dir = False
_DUP_SUFFIX_RE = re.compile(r'\s*\(\d+\)\s*$')


def _state_path() -> str:
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    return os.path.join(db_content_dir, STATE_FILENAME)


_CORRUPT = object()


def _load_state():
    """State dict, {} when there is none, or _CORRUPT after moving an unreadable
    file aside. Silently re-baselining on a corrupt file would hide every folder
    that arrived since; skipping once and baselining fresh next run is safer."""
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        err = 'not a JSON object'
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        err = str(e)
    dest = f"{path}.corrupt-{int(time.time())}"
    try:
        os.replace(path, dest)
        moved = f"moved to {dest}"
    except OSError as e:
        moved = f"could not be moved aside ({e})"
    logging.error(f"[ExternalScan] State file {path} is unreadable ({err}); {moved}. "
                  f"Skipping this run; the next run baselines fresh.")
    return _CORRUPT


def _save_state(state: Dict) -> None:
    path = _state_path()
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
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


def _name_variants(name: str, normalize) -> Set[str]:
    """Normalized name plus the form with a trailing " (N)" duplicate suffix
    removed -- the debrid account names a re-added torrent "Name (1)"."""
    out = {normalize(name)}
    stripped = _DUP_SUFFIX_RE.sub('', name)
    if stripped != name:
        out.add(normalize(stripped))
    out.discard('')
    return out


def _load_torrent_additions() -> List[Tuple[str, Dict]]:
    """(hash, item_data dict) for every torrent_additions row, read once per run
    and filtered in Python (no huge IN (...))."""
    conn = None
    out: List[Tuple[str, Dict]] = []
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        rows = conn.execute('SELECT torrent_hash, item_data FROM torrent_additions').fetchall()
        for row in rows:
            try:
                data = json.loads(row['item_data']) if row['item_data'] else {}
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                out.append(((row['torrent_hash'] or '').lower(), data))
    except Exception as e:
        logging.debug(f"[ExternalScan] torrent_additions unavailable: {e}")  # absent on a fresh install
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return out


_OURS_KEYS = ('debrid_folder_name', 'filled_by_title', 'original_scraped_torrent_title',
              'real_debrid_original_title')


def _build_ours_sets(additions: Optional[List[Tuple[str, Dict]]] = None) -> Set[str]:
    """Normalized names of every folder cli_debrid itself added to the account,
    from any torrent_additions row regardless of not-wanted status."""
    from database.database_reading import normalize_string_for_comparison
    if additions is None:
        additions = _load_torrent_additions()
    ours: Set[str] = set()
    for _hash, data in additions:
        for key in _OURS_KEYS:
            value = data.get(key)
            if value and isinstance(value, str):
                ours.update(_name_variants(value, normalize_string_for_comparison))
    return ours


def _is_ours(folder_name: str, ours_names: Set[str]) -> bool:
    if not ours_names:
        return False
    from database.database_reading import normalize_string_for_comparison
    return bool(_name_variants(folder_name, normalize_string_for_comparison) & ours_names)


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
            'SELECT filled_by_title, real_debrid_original_title, debrid_folder_name, '
            'original_scraped_torrent_title FROM media_items'
        ).fetchall()
        for row in rows:
            for value in (row['filled_by_title'], row['real_debrid_original_title'],
                          row['debrid_folder_name'], row['original_scraped_torrent_title']):
                if value:
                    known_titles.update(_name_variants(value, normalize_string_for_comparison))

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


def _build_rejected_sets(additions: Optional[List[Tuple[str, Dict]]] = None) -> Set[str]:
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
            if additions is None:
                additions = _load_torrent_additions()
            for torrent_hash, data in additions:
                if torrent_hash not in not_wanted:
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

    variants = _name_variants(folder_name, normalize_string_for_comparison)
    if variants & known_titles or variants & known_components:
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
    return bool(_name_variants(folder_name, normalize_string_for_comparison) & rejected_names)


def _should_retry(entry: Dict, now: float) -> bool:
    # Present in the mount before the feature was turned on. Never auto-import
    # these -- the whole point of the baseline is that enabling the task does
    # not drag an established library's worth of untracked content into the DB.
    if entry.get('baseline'):
        return False
    if entry.get('imported') or entry.get('given_up'):
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


def _strip_all_suffix(path: str) -> str:
    path = (path or '').rstrip('/')
    return path[:-len('/__all__')] if path.endswith('/__all__') else path


def _is_inside(child: str, parent: str) -> bool:
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _resolve_roots(mount_path: str) -> List[Dict]:
    """Scan roots: the configured mount, plus a separate Usenet Provider mount.

    Each root is {'id', 'path', 'prefix', 'swapped'}; 'usenet' roots list
    <category>/<job> layouts too.
    """
    global _warned_all_dir
    main_path, swapped = mount_path, False
    try:
        # cli_mount/decypharr/built-in/zurg are sometimes pointed at the mount
        # root; the torrent folders then sit one level down, in __all__.
        if os.path.isdir(os.path.join(mount_path, '__all__')):
            main_path, swapped = os.path.join(mount_path, '__all__'), True
            if not _warned_all_dir:
                _warned_all_dir = True
                logging.warning(f"[ExternalScan] original_files_path '{mount_path}' contains an '__all__' "
                                f"folder; scanning '{main_path}' instead. Point the setting there to silence this.")
    except OSError:
        pass
    roots = [{'id': 'main', 'path': main_path, 'prefix': '', 'swapped': swapped}]

    usenet_raw = (get_setting('Usenet Provider', 'mounted_file_location', '') or '').rstrip('/')
    usenet = _strip_all_suffix(usenet_raw)
    if usenet and os.path.isdir(usenet) and os.access(usenet, os.R_OK | os.X_OK):
        # Same/nested mounts would list every folder twice under two keys.
        overlaps = any(_is_inside(usenet, p) or _is_inside(p, usenet) for p in (mount_path, main_path))
        if not overlaps:
            # A separate cli_mount/built-in mount used for usenet has the same
            # __all__/nzbs/torrents/__bad__ layout; its jobs are in __all__, and
            # listing the mount root would see each job twice (and torrents too).
            usenet_path = os.path.join(usenet, '__all__') if os.path.isdir(os.path.join(usenet, '__all__')) else usenet
            roots.append({'id': 'usenet', 'path': usenet_path, 'prefix': USENET_PREFIX, 'swapped': False})
    return roots


def _list_root(root: Dict, state: Optional[Dict] = None) -> List[Tuple[str, str, str]]:
    """(state key, folder name, absolute path) for every importable folder."""
    if root['id'] == 'usenet':
        from usenet.mount_layout import list_job_dirs
        # Top-level names already recorded as jobs skip the category probe, so a
        # flat mount with thousands of jobs isn't listed folder by folder every run.
        prefix = root['prefix']
        known_jobs = {k[len(prefix):] for k in (state or {}) if k.startswith(prefix) and '/' not in k[len(prefix):]}
        return [(prefix + rel, os.path.basename(abs_path), abs_path)
                for rel, abs_path in list_job_dirs(root['path'], known_jobs=known_jobs)]
    return [(name, name, os.path.join(root['path'], name)) for name in _list_top_level(root['path'])]


def _root_meta(meta: Dict, root: Dict) -> Dict:
    if root['id'] == 'main':
        return meta
    return meta.setdefault(root['id'], {})


def _give_up(entry: Dict, name: str, why: str, summary: Dict) -> None:
    entry['given_up'] = True
    summary['given_up'] += 1
    logging.warning(f"[ExternalScan] giving up on '{name}' after {why}")


def _run_import(name: str, scan_path: str, symlink_base_path: str):
    """Run the pipeline; returns its progress dict."""
    from routes.debug_routes import _run_rclone_to_symlink_task, rclone_scan_progress
    task_id = str(uuid.uuid4())
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
        False,               # user_initiated - see above
        skip_known_paths=True,  # a retry must not re-insert files that already imported
    )
    return rclone_scan_progress.pop(task_id, None) or {}


def scan_mount_for_external_adds() -> Dict:
    """Scan the mount(s) for folders cli_debrid has no record of and import them.

    Returns a summary dict for logging. Never runs concurrently with itself.
    """
    summary = {
        'skipped_reason': None,
        'entries_seen': 0,
        'new_candidates': 0,
        'imported': 0,
        'failed': 0,
        'baselined': 0,
        'rejected': 0,
        'pending': 0,
        'ours': 0,
        'given_up': 0,
    }
    if not _SCAN_LOCK.acquire(blocking=False):
        summary['skipped_reason'] = 'previous external scan still running'
        return summary
    try:
        _scan(summary)
    finally:
        _SCAN_LOCK.release()
    return summary


def _scan(summary: Dict) -> None:
    if get_setting('File Management', 'file_collection_management', 'Symlinked/Local') != 'Symlinked/Local':
        summary['skipped_reason'] = 'file collection management is not Symlinked/Local'
        return

    mount_path = get_setting('File Management', 'original_files_path', '')
    if not mount_path or not os.path.isdir(mount_path):
        summary['skipped_reason'] = f"original_files_path '{mount_path}' is not a readable directory"
        return

    symlink_base_path = get_setting('File Management', 'symlinked_files_path', '')
    if not symlink_base_path:
        summary['skipped_reason'] = 'symlinked_files_path is not configured'
        return

    state = _load_state()
    if state is _CORRUPT:
        summary['skipped_reason'] = 'state file was corrupt; moved aside, re-baselining next run'
        return

    now = time.time()
    # Only write the state file when something actually changed. The steady
    # state is a large mount where every entry is already known, and rewriting
    # a file with one key per mount folder on every run buys nothing.
    dirty = False

    meta = state.get(META_KEY)
    if not isinstance(meta, dict):
        # Old state files have no meta; they keep working, it is added now.
        meta = state[META_KEY] = {}
        dirty = True
    prev_last_run = meta.get('last_run')
    long_gap = bool(prev_last_run) and (now - prev_last_run) > LONG_GAP_SECONDS
    if not prev_last_run or now - prev_last_run >= LAST_RUN_WRITE_INTERVAL:
        dirty = True
    meta['last_run'] = now

    candidates = []  # (key, name, abs path)
    for root in _resolve_roots(mount_path):
        entries = _list_root(root, state)
        rm = _root_meta(meta, root)
        prev_count = rm.get('last_entry_count')
        if prev_count and prev_count >= FLAKY_MIN_COUNT and len(entries) < prev_count * FLAKY_RATIO:
            low_runs = rm.get('low_listing_runs', 0) + 1
            rm['low_listing_runs'] = low_runs
            dirty = True
            if low_runs < FLAKY_ACCEPT_AFTER:
                # A half-listed mount would make the pruning below forget folders and
                # the rest look new; wait for a sane listing.
                logging.warning(f"[ExternalScan] '{root['path']}' listed {len(entries)} entries "
                                f"(previously {prev_count}); looks like a flaky mount, skipping this root "
                                f"({low_runs}/{FLAKY_ACCEPT_AFTER}).")
                continue
            logging.warning(f"[ExternalScan] '{root['path']}' has listed {len(entries)} entries "
                            f"(previously {prev_count}) {low_runs} runs in a row; accepting the new count.")
        if rm.pop('low_listing_runs', None) is not None:
            dirty = True
        if not entries:
            continue
        summary['entries_seen'] += len(entries)

        keys = {k for k, _, _ in entries}
        root_keys = [k for k in state if k != META_KEY and k.startswith(USENET_PREFIX) == (root['id'] == 'usenet')]
        first_run = not rm.get('baselined_at') and not root_keys
        # The scan path changing (or an '__all__' step-in on a state recorded
        # against the mount root) makes every folder name look new.
        old_path = rm.get('scan_path')
        relocated = (old_path is not None and old_path != root['path']) or \
                    (old_path is None and root['swapped'] and bool(root_keys))
        if first_run or relocated or long_gap:
            added = 0
            for key, _name, _p in entries:
                if key not in state:
                    state[key] = {'imported': False, 'baseline': True, 'first_seen': now}
                    added += 1
            if added:
                dirty = True
                summary['baselined'] += added
                if first_run:
                    logging.info(
                        f"[ExternalScan] First run for '{root['path']}' - recorded {added} existing mount "
                        f"entries as baseline without importing. Content appearing from now on will be "
                        f"imported. Delete {_state_path()} to re-baseline.")
                else:
                    why = 'the scan path changed' if relocated else f"the task was off for over {LONG_GAP_SECONDS // 86400} days"
                    logging.info(f"[ExternalScan] Baselined {added} unknown folders under '{root['path']}' "
                                 f"instead of importing them ({why}).")
        if not rm.get('baselined_at'):
            rm['baselined_at'] = now
            dirty = True
        if rm.get('scan_path') != root['path']:
            rm['scan_path'] = root['path']
            dirty = True
        if rm.get('last_entry_count') != len(entries):
            rm['last_entry_count'] = len(entries)
            dirty = True

        # Forget folders that have been gone from the mount for a while, so the
        # file doesn't grow without bound -- but not on a single missed listing.
        for key in root_keys:
            entry = state[key]
            if key in keys:
                if isinstance(entry, dict) and entry.pop('gone_since', None) is not None:
                    dirty = True
                continue
            if not isinstance(entry, dict):
                del state[key]
                dirty = True
                continue
            gone_since = entry.get('gone_since')
            if gone_since is None:
                entry['gone_since'] = now
                dirty = True
            elif now - gone_since >= GONE_GRACE_SECONDS:
                del state[key]
                dirty = True

        for key, name, path in entries:
            entry = state.get(key)
            if entry is not None and not _should_retry(entry, now):
                continue
            candidates.append((key, name, path))

    def finish():
        if dirty:
            _save_state(state)

    if not candidates:
        finish()
        return

    known_titles, known_components = _build_known_sets()
    additions = _load_torrent_additions()
    rejected_names = _build_rejected_sets(additions)
    ours_names = _build_ours_sets(additions)

    ready = []
    for key, name, path in candidates:
        if _already_tracked(name, known_titles, known_components):
            # Tracked by cli_debrid already - record it so we don't re-check
            # it against the DB on every run.
            state[key] = {'imported': True, 'tracked_existing': True, 'first_seen': now}
            dirty = True
            continue
        is_ours = _is_ours(name, ours_names)
        if is_ours or _is_rejected(name, rejected_names):
            # cli_debrid's own leftover (a torrent it added itself, or a release
            # it blacklisted/ghostlisted), not an external add: importing it
            # would reinstate exactly what the queues rejected.
            entry = state.get(key) or {'first_seen': now}
            entry['rejected'] = True
            entry['last_attempt'] = now
            if is_ours:
                entry['ours'] = True
                summary['ours'] += 1
            else:
                summary['rejected'] += 1
            state[key] = entry
            dirty = True
            logging.info(f"[ExternalScan] Skipping '{name}' - "
                         f"{'a torrent cli_debrid added itself' if is_ours else 'blacklisted/not-wanted content cli_debrid rejected itself'}, "
                         f"not an external add.")
            continue

        entry = state.get(key)
        if entry is None:
            # First sight: record only. It may still be mid-upload.
            state[key] = {'first_seen': now, 'seen_runs': 1, 'pending': True}
            dirty = True
            summary['pending'] += 1
            continue
        if entry.get('rejected'):
            # Was junk, no longer matches (e.g. un-blacklisted): treat as new.
            entry.pop('rejected', None)
            entry.pop('ours', None)
        seen_runs = entry.get('seen_runs', 2)  # old entries predate settling
        entry['seen_runs'] = seen_runs + 1
        dirty = True
        if seen_runs + 1 < 2 or now - entry.get('first_seen', 0) < SETTLE_SECONDS:
            summary['pending'] += 1
            continue
        state[key] = entry
        ready.append((key, name, path))

    summary['new_candidates'] = len(ready)
    if not ready:
        finish()
        return

    logging.info(f"[ExternalScan] {len(ready)} mount entries with no database record; "
                 f"importing up to {MAX_IMPORTS_PER_RUN} this run.")

    for key, name, scan_path in ready[:MAX_IMPORTS_PER_RUN]:
        entry = state[key]
        first_seen = entry.get('first_seen', now)
        if entry.get('no_media') and now - first_seen >= PENDING_GIVE_UP_SECONDS:
            _give_up(entry, name, '24h with no media files', summary)
            continue

        logging.info(f"[ExternalScan] Importing external add: {scan_path}")
        attempt_used = True
        try:
            progress = _run_import(name, scan_path, symlink_base_path)
            added = progress.get('items_added_to_db', 0)
            symlinks = progress.get('symlinks_created', 0)
            problems = [f"{label} {progress.get(k, 0)}" for k, label in (
                ('parser_errors', 'parser'), ('metadata_errors', 'metadata'),
                ('db_errors', 'db'), ('symlink_errors', 'symlink')) if progress.get(k, 0)]
            if progress.get('status') == 'error':
                problems.append('task error')
            already = progress.get('skipped_previously_processed', 0)

            if not problems and added == 0 and already == 0 and progress.get('media_files_found', 0) == 0:
                # Nothing to import yet (files not listed, or still uploading):
                # not a failure, so no attempt is consumed.
                attempt_used = False
                entry['no_media'] = True
                summary['pending'] += 1
                logging.info(f"[ExternalScan] '{name}' has no media files yet; will retry next run.")
                if now - first_seen >= PENDING_GIVE_UP_SECONDS:
                    _give_up(entry, name, '24h with no media files', summary)
            elif not problems and (added > 0 or already > 0):
                entry.pop('no_media', None)
                entry['imported'] = True
                summary['imported'] += 1
                logging.info(f"[ExternalScan] Imported '{name}' - {added} item(s) added, "
                             f"{symlinks} symlink(s) created.")
            else:
                entry.pop('no_media', None)
                summary['failed'] += 1
                logging.warning(
                    f"[ExternalScan] '{name}' import incomplete "
                    f"(attempt {entry.get('attempts', 0) + 1}/{MAX_ATTEMPTS}"
                    f"{'; ' + ', '.join(problems) if problems else ''}; {added} added): "
                    f"{progress.get('message', 'no progress reported')}"
                )
        except Exception as e:
            summary['failed'] += 1
            logging.error(f"[ExternalScan] Import failed for '{name}': {e}", exc_info=True)

        if attempt_used:
            entry['attempts'] = entry.get('attempts', 0) + 1
            entry['last_attempt'] = now
            if not entry.get('imported') and entry['attempts'] >= MAX_ATTEMPTS:
                _give_up(entry, name, f"{entry['attempts']} attempts", summary)
        dirty = True

    finish()
