"""Re-point symlinks left dangling by a cli_mount rename.

Debrid naming renames a cli_mount entry shortly after the torrent is added, and for a
single-file entry cli_mount renames the file too ("<new name><ext>"); the folder follows
only when cli_mount's folder naming is file-name based. If the checking queue finds and
symlinks the file in the seconds before the rename lands, the symlink keeps pointing at
the old path, which no longer exists. This finds those symlinks for the renamed entry and
points them at whichever renamed path now exists.
"""

import logging
import os
import threading
import time
from typing import List, Optional

from utilities.settings import get_setting

_ATTEMPTS = 6
_DELAY_SECONDS = 10


def _candidate_targets(old_target: str, new_name: str) -> List[str]:
    old_dir, old_file = os.path.split(old_target)
    root = os.path.dirname(old_dir)
    ext = os.path.splitext(old_file)[1]
    stem = new_name[:-4] if new_name.lower().endswith('.nzb') else new_name
    new_file = stem if not ext or stem.lower().endswith(ext.lower()) else stem + ext
    candidates = []
    for path in (
        os.path.join(root, stem, new_file),  # folder and file renamed (file-name folder naming)
        os.path.join(old_dir, new_file),      # only the file renamed (original-name folder naming)
        os.path.join(root, stem, old_file),   # only the folder renamed (multi-file entries)
    ):
        if path != old_target and path not in candidates:
            candidates.append(path)
    return candidates


def _dangling_symlinks(info_hash: str) -> List[dict]:
    from database.core import get_db_connection

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, location_on_disk FROM media_items "
            "WHERE state IN ('Collected', 'Upgrading') "
            "AND location_on_disk IS NOT NULL AND location_on_disk != '' "
            "AND (filled_by_magnet LIKE ? OR filled_by_torrent_id = ?)",
            (f'%{info_hash}%', f'nzb:{info_hash}'),
        ).fetchall()
    dangling = []
    for row in rows:
        link = row['location_on_disk']
        if not os.path.islink(link):
            continue
        target = os.readlink(link)
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(os.path.dirname(link), target))
        if not os.path.exists(target):
            dangling.append({'id': row['id'], 'link': link, 'target': target})
    return dangling


def _repoint(entry: dict, new_target: str) -> None:
    from database.database_writing import update_media_item

    tmp_link = entry['link'] + '.cli_debrid_tmp'
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(new_target, tmp_link)
    os.replace(tmp_link, entry['link'])
    update_media_item(
        entry['id'],
        original_path_for_symlink=new_target,
        filled_by_file=os.path.basename(new_target),
        debrid_folder_name=os.path.basename(os.path.dirname(new_target)),
    )
    logging.info(f"[DebridNaming] Re-pointed symlink for item {entry['id']} after rename: {entry['link']} -> {new_target}")


def repoint_symlinks_after_rename(info_hash: str, new_name: str, attempts: int = _ATTEMPTS,
                                  delay_seconds: float = _DELAY_SECONDS) -> int:
    """Re-point dangling symlinks of items filled by info_hash. Returns how many were fixed.

    Retries for a short while because the mount can take a moment to show the new name.
    """
    if get_setting('File Management', 'file_collection_management') != 'Symlinked/Local':
        return 0
    if not info_hash or not new_name:
        return 0

    fixed = 0
    pending: Optional[List[dict]] = None
    for attempt in range(attempts):
        try:
            pending = _dangling_symlinks(info_hash) if pending is None else pending
        except Exception as e:
            logging.debug(f"[DebridNaming] Could not look up symlinks for {info_hash}: {e}")
            return fixed
        if not pending:
            return fixed
        still_pending = []
        for entry in pending:
            new_target = next((p for p in _candidate_targets(entry['target'], new_name) if os.path.exists(p)), None)
            if not new_target:
                still_pending.append(entry)
                continue
            try:
                _repoint(entry, new_target)
                fixed += 1
            except Exception as e:
                logging.warning(f"[DebridNaming] Could not re-point symlink for item {entry['id']}: {e}")
        pending = still_pending
        if pending and attempt < attempts - 1:
            time.sleep(delay_seconds)
    if pending:
        logging.warning(
            f"[DebridNaming] {len(pending)} symlink(s) for {info_hash} still point at a missing file after rename to {new_name!r}: "
            + ', '.join(e['link'] for e in pending)
        )
    return fixed


def repoint_symlinks_after_rename_async(info_hash: str, new_name: str) -> None:
    threading.Thread(
        target=repoint_symlinks_after_rename, args=(info_hash, new_name),
        name=f'debrid-rename-repoint-{info_hash[:8]}', daemon=True,
    ).start()
