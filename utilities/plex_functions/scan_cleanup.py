"""Post-scan cleanup of replaced/dead Plex versions and Plex trash."""

from utilities.settings import get_setting
import time
import plexapi.server
import os
from ._common import logger
from .removal import _find_plex_owner_of_file, remove_file_from_plex

def plex_connection_settings():
    """(url, token) of the Plex server for the file-management mode, '' when unset.

    Plex mode uses Plex.url/token, or the *_for_symlink fields when those are
    blank (both hold the same server when filled). Symlink mode uses only the
    *_for_symlink fields: leaving them blank there means "don't scan Plex", even
    when Plex.url is set for watchlists.
    """
    symlink = ((get_setting('File Management', 'plex_url_for_symlink', default='') or '').strip().rstrip('/'),
               (get_setting('File Management', 'plex_token_for_symlink', default='') or '').strip())
    if get_setting('File Management', 'file_collection_management') != 'Plex':
        return symlink if all(symlink) else ('', '')
    main = ((get_setting('Plex', 'url') or '').strip().rstrip('/'), (get_setting('Plex', 'token') or '').strip())
    return main if all(main) else symlink if all(symlink) else ('', '')

def _file_management_plex():
    """PlexServer for the configured file-management mode, or None if not configured."""
    plex_url, plex_token = plex_connection_settings()
    if not plex_url or not plex_token:
        return None
    return plexapi.server.PlexServer(plex_url, plex_token, timeout=30)

def _wait_for_plex_file(plex, item_title, item_path, episode_title, timeout, interval):
    """Poll until Plex has item_path under item_title; returns (owner, media) or (None, None)."""
    deadline = time.time() + timeout
    while True:
        try:
            owner, media = _find_plex_owner_of_file(plex, item_title, item_path, episode_title)
            if owner is not None:
                return owner, media
        except Exception as e:
            logger.debug(f"[REPLACE_PLEX] Plex lookup for {item_title} failed, retrying: {e}")
        if time.time() >= deadline:
            return None, None
        time.sleep(interval)

def remove_replaced_plex_media_after_scan(item_title, new_path, old_paths, episode_title=None,
                                          timeout=300, interval=10):
    """Symlinked/Local Replace: drop the old version from Plex only after the new file is in.

    The replacement is a different file in the same folder, so Plex's scan attaches it to the
    existing movie/episode as a second version. Deleting the old (only) version before that
    deletes the whole item, and the replacement then comes back as a brand-new "recently added"
    item with a fresh addedAt. Waiting first means only the old version is removed and the item
    keeps its addedAt and watch state. Removing it explicitly also matters when Plex's
    "empty trash automatically" is off, which would otherwise leave it as an unavailable version.

    On timeout the old versions are removed anyway (the previous behaviour), so a stalled scan
    can't leave dead versions behind.
    """
    try:
        plex = _file_management_plex()
        if plex is None:
            logger.warning(f"[REPLACE_PLEX] No Plex URL/token configured; cannot remove replaced version of {item_title}")
            return False

        owner, _ = _wait_for_plex_file(plex, item_title, new_path, episode_title, timeout, interval)
        if owner is not None:
            logger.info(f"[REPLACE_PLEX] Plex picked up {os.path.basename(new_path)}; removing replaced version(s) of {item_title}")
        else:
            logger.warning(f"[REPLACE_PLEX] Plex hadn't picked up {os.path.basename(new_path)} after {timeout}s; "
                           f"removing replaced version(s) of {item_title} anyway")

        removed = True
        for old_path in old_paths:
            if not remove_file_from_plex(item_title, old_path, episode_title):
                removed = False
                logger.warning(f"[REPLACE_PLEX] Could not remove replaced version {os.path.basename(old_path)} of {item_title} from Plex")
        return removed
    except Exception as e:
        logger.error(f"[REPLACE_PLEX] Error removing replaced version of {item_title}: {e}", exc_info=True)
        return False

def _paths_tracked_in_db(paths):
    """The subset of paths (normalized) that a media item still has as its location_on_disk.

    Raises on a DB error so the caller can skip rather than guess."""
    from database import get_db_connection
    wanted = {os.path.normpath(p) for p in paths}
    if not wanted:
        return set()
    conn = get_db_connection()
    try:
        placeholders = ','.join('?' * len(wanted))
        rows = conn.execute(
            f"SELECT location_on_disk FROM media_items WHERE location_on_disk IN ({placeholders})",
            list(wanted),
        ).fetchall()
        return {os.path.normpath(row[0]) for row in rows if row[0]}
    finally:
        conn.close()

def remove_dead_plex_versions_after_scan(item_title, new_path, episode_title=None, timeout=300, interval=10):
    """After a repaired/replaced item is re-collected, drop the versions it left behind.

    Repairs replace the file under a new name in the same folder (the default symlink
    template includes {original_filename}), so Plex shows the replacement as a second
    version of the same item. The old version's symlink is left dangling (or already
    unlinked) and, with Plex's "empty trash automatically" off, stays as an unavailable
    version forever.

    Once Plex has new_path, every OTHER version of that same Plex item that lives in the
    same folder and whose file no longer exists on disk is removed, along with its dangling
    symlink. A version whose file still exists is never touched (real duplicates, or an old
    file a repair hasn't cleaned up yet), and new_path resolving proves the mount is up, so
    an outage can't make live files look dead. new_path only proves its own mount is up, so a
    version that a media item still has as its location_on_disk (e.g. a symlink into another
    mount that is down right now) is also left alone. Returns the number of versions removed.
    """
    try:
        local_dir = os.path.dirname(new_path)
        if not os.path.exists(new_path):
            logger.info(f"[PLEX_DEAD_VERSIONS] {new_path} doesn't resolve; skipping dead-version cleanup for {item_title}")
            return 0
        plex = _file_management_plex()
        if plex is None:
            return 0
        owner, new_media = _wait_for_plex_file(plex, item_title, new_path, episode_title, timeout, interval)
        if owner is None:
            logger.info(f"[PLEX_DEAD_VERSIONS] Plex hasn't picked up {os.path.basename(new_path)} after {timeout}s; skipping {item_title}")
            return 0
        plex_dir = os.path.dirname(new_media.parts[0].file)
        candidates = []
        for media in list(owner.media):
            if media is new_media or media.id == new_media.id:
                continue
            files = [part.file for part in media.parts]
            # Same folder as the new file in Plex's own path terms, so the local path is
            # unambiguous whatever path mapping Plex and cli_debrid use.
            if not files or any(os.path.dirname(f) != plex_dir for f in files):
                continue
            local_files = [os.path.join(local_dir, os.path.basename(f)) for f in files]
            if any(os.path.exists(f) for f in local_files):
                continue
            candidates.append((media, files, local_files))
        if not candidates:
            return 0
        tracked = _paths_tracked_in_db(f for _, _, local_files in candidates for f in local_files)
        removed = 0
        for media, files, local_files in candidates:
            if any(os.path.normpath(f) in tracked for f in local_files):
                logger.info(f"[PLEX_DEAD_VERSIONS] {os.path.basename(files[0])} of {item_title} doesn't resolve but "
                            f"is still a media item's location_on_disk (its mount may be down); leaving it")
                continue
            label = os.path.basename(files[0])
            try:
                media.delete()
                removed += 1
                logger.info(f"[PLEX_DEAD_VERSIONS] Removed dead Plex version {label} of {item_title}")
            except Exception as e:
                logger.warning(f"[PLEX_DEAD_VERSIONS] Could not remove dead Plex version {label} of {item_title}: {e}")
                continue
            for f in local_files:
                if os.path.islink(f):  # dangling, checked above
                    try:
                        os.unlink(f)
                        logger.info(f"[PLEX_DEAD_VERSIONS] Removed dangling symlink {f}")
                    except OSError as e:
                        logger.warning(f"[PLEX_DEAD_VERSIONS] Could not remove dangling symlink {f}: {e}")
        return removed
    except Exception as e:
        logger.error(f"[PLEX_DEAD_VERSIONS] Error cleaning dead versions of {item_title}: {e}", exc_info=True)
        return 0

def start_dead_plex_version_cleanup(item):
    """Run remove_dead_plex_versions_after_scan for a collected item in the background.

    Symlinked/Local with Plex only (Jellyfin/Emby and Plex mode are left alone)."""
    try:
        if get_setting('File Management', 'file_collection_management') != 'Symlinked/Local':
            return False
        if (get_setting('Debug', 'emby_jellyfin_url', default='') or '').strip():
            return False
        from utilities.silo_functions import is_silo_selected
        if is_silo_selected():
            return False
        new_path = item.get('location_on_disk')
        if not new_path:
            return False
        import threading
        threading.Thread(
            target=remove_dead_plex_versions_after_scan,
            args=(item.get('title'), new_path, item.get('episode_title')),
            name=f"plex-dead-versions-{item.get('id')}",
            daemon=True,
        ).start()
        return True
    except Exception as e:
        logger.warning(f"[PLEX_DEAD_VERSIONS] Could not start cleanup for {item.get('title')}: {e}")
        return False

def scan_and_empty_plex_trash(paths: list = None, section_type: str = None, empty_trash: bool = True) -> dict:
    """
    Scan specific Plex paths and then empty trash to clean up unavailable items.

    The scan is needed because Plex doesn't move items to trash until it detects
    the files are missing during a library scan.

    Args:
        paths: List of specific folder paths to scan (e.g., deleted symlink parent folders).
               If provided, only these paths will be scanned instead of entire sections.
        section_type: Optional - 'movie' or 'show' to filter sections for trash empty.
                      If None, empties trash for all sections.

    Returns:
        dict with 'success', 'paths_scanned', 'sections_cleaned', 'errors'
    """
    import time

    result = {
        'success': True,
        'paths_scanned': [],
        'sections_cleaned': [],
        'errors': []
    }

    # Silo picked as the media server: a rescan of each path drops what's gone
    # (Silo has no trash to empty)
    from utilities.silo_functions import is_silo_mode, scan_path
    if is_silo_mode():
        silo_type = 'movie' if section_type == 'movie' else 'episode'
        for path in paths or []:
            (result['paths_scanned'] if scan_path(path, silo_type) else result['errors']).append(path)
        result['success'] = not result['errors']
        return result

    # Direct Mount run with Jellyfin/Emby: same, through its library folders
    from utilities.jellyfin_direct import is_jellyfin_direct, scan_path as jellyfin_scan_path
    if is_jellyfin_direct():
        jf_type = 'movie' if section_type == 'movie' else 'episode'
        for path in paths or []:
            (result['paths_scanned'] if jellyfin_scan_path(path, jf_type, removed=True) else result['errors']).append(path)
        result['success'] = not result['errors']
        return result

    try:
        # Check for Jellyfin configuration first
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url', default='').strip()
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token', default='').strip()

        if jellyfin_url and jellyfin_token:
            logger.info("Jellyfin configured - skipping Plex scan and trash empty")
            result['sections_cleaned'].append('Jellyfin (skipped)')
            return result

        # Get Plex connection based on file management mode
        if get_setting('File Management', 'file_collection_management') == 'Plex':
            plex_url = get_setting('Plex', 'url').rstrip('/')
            plex_token = get_setting('Plex', 'token')
        elif get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
            plex_url = get_setting('File Management', 'plex_url_for_symlink', default='')
            plex_token = get_setting('File Management', 'plex_token_for_symlink', default='')
        else:
            logger.warning("No Plex URL or token found in settings for trash empty")
            result['errors'].append("No Plex configuration found")
            result['success'] = False
            return result

        if not plex_url or not plex_token:
            logger.warning("Plex URL or token is empty")
            result['errors'].append("Plex URL or token is empty")
            result['success'] = False
            return result

        plex = plexapi.server.PlexServer(plex_url, plex_token, timeout=30)
        sections = plex.library.sections()

        # Track which sections were actually scanned
        scanned_sections = set()

        # Step 1: Scan specific paths or sections
        if paths:
            # Scan only the specific paths provided (much faster than full library scan)
            for path in paths:
                try:
                    # Find which section contains this path
                    for section in sections:
                        if not hasattr(section, 'locations') or not section.locations:
                            continue
                        for location in section.locations:
                            if path.startswith(location):
                                logger.info(f"Scanning specific path in Plex: {path} (section: {section.title})")
                                section.update(path=path)
                                result['paths_scanned'].append(path)
                                scanned_sections.add(section.key)  # Track this section
                                logger.info(f"Successfully triggered scan for path: {path}")
                                break
                except Exception as e:
                    error_msg = f"Failed to scan path {path}: {str(e)}"
                    logger.error(error_msg)
                    result['errors'].append(error_msg)
        else:
            # No specific paths - scan entire sections (fallback, slower)
            for section in sections:
                try:
                    if section_type:
                        if section_type == 'movie' and section.type != 'movie':
                            continue
                        if section_type == 'show' and section.type != 'show':
                            continue

                    logger.info(f"Scanning entire Plex section: {section.title} ({section.type})")
                    section.update()
                    result['paths_scanned'].append(f"[section] {section.title}")
                    scanned_sections.add(section.key)  # Track this section
                    logger.info(f"Successfully triggered scan for section: {section.title}")

                except Exception as e:
                    error_msg = f"Failed to scan section {section.title}: {str(e)}"
                    logger.error(error_msg)
                    result['errors'].append(error_msg)

        # Step 2: Wait for scan to process (Plex needs time to detect missing files)
        if result['paths_scanned']:
            logger.info("Waiting for Plex to process scan...")
            time.sleep(2)

        # Step 3: Empty trash ONLY for sections that were actually scanned
        if not empty_trash:
            return result

        # Safety: if specific paths were requested but none of them matched any
        # Plex library section (e.g. a debrid-mount path that doesn't match the
        # section's Plex-visible location), scanned_sections stays empty. Emptying
        # trash in that case would fall through to "every section" below, wiping
        # unrelated content across the whole library instead of the intended item.
        # Fail closed: skip the trash-empty entirely rather than guess.
        if paths and not scanned_sections:
            error_msg = f"No Plex section matched any of the requested paths {paths!r} — skipping trash empty to avoid affecting unrelated sections"
            logger.warning(error_msg)
            result['success'] = False
            result['errors'].append(error_msg)
            return result

        for section in sections:
            try:
                # Skip if this section wasn't scanned
                if scanned_sections and section.key not in scanned_sections:
                    continue

                # Additional filter by section type if specified (backup safety check)
                if section_type:
                    if section_type == 'movie' and section.type != 'movie':
                        continue
                    if section_type == 'show' and section.type != 'show':
                        continue

                logger.info(f"Emptying trash for Plex section: {section.title} ({section.type})")
                section.emptyTrash()
                result['sections_cleaned'].append(section.title)
                logger.info(f"Successfully emptied trash for section: {section.title}")

            except Exception as e:
                error_msg = f"Failed to empty trash for section {section.title}: {str(e)}"
                logger.error(error_msg)
                result['errors'].append(error_msg)

        if result['errors']:
            result['success'] = len(result['sections_cleaned']) > 0

    except Exception as e:
        logger.error(f"Error during Plex scan and trash empty: {str(e)}")
        result['errors'].append(str(e))
        result['success'] = False

    return result
