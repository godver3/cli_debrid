"""Bulk subtitles, duplicate symlinks, episode-number fixes, collected-watchlist cleanup."""

from flask import jsonify, request
from utilities.settings import get_setting
import logging
from routes import admin_required
from datetime import datetime
from debrid import get_debrid_provider
from utilities.deletion_manager import DeletionManager
from routes.program_operation_routes import get_program_runner
from database.database_writing import RESET_COLLECTION_STATE_SQL
from ._common import debug_bp

def async_run_bulk_subs():
    """Queue a subtitle search for every collected item missing a language,
    on the same search lane as Tools → Subtitle Manager → Search all missing
    (history, skip rules and provider pauses shared; progress shown there).
    Replaces the old bulk_subs.sh run, which kept its own .subs-processed
    flag files and saw only Symlinked/Local sidecars."""
    try:
        from utilities import subtitle_bulk, subtitle_status
        from utilities.settings import get_setting
        if not get_setting('Subtitle Settings', 'enable_subtitles', False):
            return {'success': False, 'error': 'Subtitle downloads are off (Settings → Subtitles).'}
        items = subtitle_status.missing_items()
        if not items:
            return {'success': True, 'message': 'No collected items are missing subtitles.'}
        how, n = subtitle_bulk.start(items, label='Bulk subtitle scan (Debug)', kind='search')
        message = (f"{'Started' if how == 'started' else 'Queued'} a subtitle search for {n} item(s); "
                   "progress is on Tools → Subtitle Manager.")
        logging.info(f"[subs] {message}")
        return {'success': True, 'message': message}
    except Exception as e:
        logging.error(f"Exception during bulk subtitle scan: {e}", exc_info=True)
        return {'success': False, 'error': f"An unexpected error occurred: {str(e)}"}

@debug_bp.route('/run_bulk_subtitle_scan', methods=['POST'])
@admin_required
def run_bulk_subtitle_scan():
    """API endpoint to trigger the bulk subtitle scan task."""
    from routes.extensions import task_queue
    task_id = task_queue.add_task(async_run_bulk_subs)
    return jsonify({'task_id': task_id}), 202

@debug_bp.route('/api/scan_duplicate_symlinks', methods=['POST'])
@admin_required
def scan_duplicate_symlinks():
    """Read-only scan: find TV episode symlinks that point at the same underlying
    file. Usually means a season pack was mismatched during collection and
    multiple "different" episodes actually got symlinked to the same single
    file instead of their own - so playing S01E04 actually plays S01E01, etc.
    Makes no changes; rescraping affected items is a separate explicit action."""
    import os
    from collections import defaultdict
    from database import get_db_connection

    try:
        symlink_root = get_setting('File Management', 'symlinked_files_path', '')
        if not symlink_root:
            return jsonify({'success': False, 'error': 'no symlinked_files_path configured'}), 400

        tv_folder_names = [get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')]
        if get_setting('Debug', 'enable_separate_anime_folders', False):
            tv_folder_names.append(get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows'))

        tv_roots = [os.path.join(symlink_root, name) for name in tv_folder_names]
        existing_roots = [r for r in tv_roots if os.path.isdir(r)]
        if not existing_roots:
            return jsonify({'success': False, 'error': f'No TV symlink folders found (checked: {", ".join(tv_roots)})'}), 400

        by_target = defaultdict(list)
        scanned = 0
        for tv_root in existing_roots:
            for dirpath, dirnames, filenames in os.walk(tv_root):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if not os.path.islink(full):
                        continue
                    scanned += 1
                    try:
                        target = os.readlink(full)
                    except OSError:
                        continue
                    by_target[target].append(full)

        dupe_groups = {t: paths for t, paths in by_target.items() if len(paths) > 1}

        conn = get_db_connection()
        groups_out = []
        for target, paths in dupe_groups.items():
            items = []
            for p in paths:
                row = conn.execute(
                    "SELECT id, title, year, season_number, episode_number FROM media_items WHERE location_on_disk = ?",
                    (p,)
                ).fetchone()
                # Only items matched to a real database row are actionable from
                # this UI (nothing to safely rescrape otherwise) - drop the rest
                # rather than showing an inert "not matched" row.
                if not row:
                    continue
                items.append({
                    'path': p,
                    'item_id': row['id'],
                    'title': row['title'],
                    'year': row['year'],
                    'season_number': row['season_number'],
                    'episode_number': row['episode_number'],
                })
            if len(items) > 1:
                # Distinguish a genuine mismatch (different episodes wrongly sharing a file)
                # from a legitimate combined multi-episode release file (e.g. a premiere
                # packaged as one S01E01E02.mkv covering both episodes) - same episode/season
                # numbers on both sides is the signal either way, but only the latter is
                # expected/harmless. Reuse the same title parser the scraper already uses for
                # ranking (parse_with_ptt) rather than writing new episode-detection logic.
                is_multi_episode_release = False
                try:
                    from scraper.functions.ptt_parser import parse_with_ptt
                    target_basename = os.path.basename(target)
                    parsed = parse_with_ptt(target_basename)
                    parsed_episodes = set(parsed.get('episodes') or [])
                    group_episodes = {i['episode_number'] for i in items if i.get('episode_number') is not None}
                    if group_episodes and parsed_episodes == group_episodes:
                        is_multi_episode_release = True  # "possible" - PTT parsing isn't infallible, still shown for review either way
                except Exception as e:
                    logging.debug(f"Multi-episode release detection failed for {target}: {e}")
                groups_out.append({
                    'target': target,
                    'items': items,
                    'is_multi_episode_release': is_multi_episode_release,
                })
        conn.close()

        groups_out.sort(key=lambda g: -len(g['items']))

        return jsonify({
            'success': True,
            'scanned': scanned,
            'group_count': len(groups_out),
            'affected_count': sum(len(g['items']) for g in groups_out),
            'groups': groups_out,
        })
    except Exception as e:
        logging.error(f"Error scanning for duplicate symlink targets: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/rescrape_duplicate_symlinks', methods=['POST'])
@admin_required
def rescrape_duplicate_symlinks():
    """Fully delete the given items (media server entry, symlink, source file/
    debrid-or-usenet download, cache) the same way deleting an episode from the
    library does, then move them back to Wanted so they get rescraped fresh.
    Deletion runs as one batch via DeletionManager.delete_multiple_items, whose
    sibling guard already excludes other items in the same batch when deciding
    whether a shared season-pack torrent/NZB is still needed elsewhere - so
    rescraping an entire duplicate-target group in one go won't have the items
    block each other's cleanup."""
    from database import get_db_connection

    try:
        data = request.get_json(silent=True) or {}
        item_ids = data.get('item_ids') or []
        if not item_ids:
            return jsonify({'success': False, 'error': 'No item_ids provided'}), 400

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=False,
            blacklist_sources=False,
            delete_from_media_server=True,
            delete_files=True,
            delete_from_debrid=True,
            delete_symlinks=True,
            clear_cache=True,
            remove_from_content_source=False,
            skip_database=True  # We reset state to Wanted ourselves below instead of deleting the row
        )

        if not result.get('success'):
            errors_list = result.get('errors', ['Deletion failed'])
            logging.error(f"Error deleting duplicate-symlink items before rescrape: {errors_list}")
            return jsonify({'success': False, 'error': errors_list[0] if errors_list else 'Deletion failed', 'errors': errors_list}), 500

        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now()
        moved = 0
        for item_id in item_ids:
            cursor.execute(f'''
                UPDATE media_items
                SET state = 'Wanted',
                    filled_by_file = NULL,
                    filled_by_title = NULL,
                    filled_by_magnet = NULL,
                    filled_by_torrent_id = NULL,
                    {RESET_COLLECTION_STATE_SQL},
                    last_updated = ?,
                    disable_not_wanted_check = TRUE,
                    location_on_disk = NULL,
                    original_path_for_symlink = NULL,
                    original_scraped_torrent_title = NULL,
                    upgrading_from = NULL,
                    version = TRIM(version, '*'),
                    upgrading = NULL
                WHERE id = ?
                AND state NOT IN ('Wanted', 'Scraping', 'Adding')
            ''', (now, item_id))
            moved += cursor.rowcount
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'moved': moved})
    except Exception as e:
        logging.error(f"Error rescraping duplicate-symlink items: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/remove_duplicate_items', methods=['POST'])
@admin_required
def remove_duplicate_items():
    try:
        from database.database_writing import remove_duplicate_items as _remove_duplicate_items
        dry_run = request.form.get('dry_run') == 'on'
        nas_filter = request.form.get('nas_filter', 'all')  # 'all', 'exclude_nas', 'only_nas'

        # Parse exclude patterns (comma or pipe separated)
        exclude_patterns_raw = request.form.get('exclude_patterns', '').strip()
        exclude_patterns = []
        if exclude_patterns_raw:
            exclude_patterns = [p.strip() for p in exclude_patterns_raw.replace('|', ',').split(',') if p.strip()]

        return jsonify(_remove_duplicate_items(dry_run=dry_run, nas_filter=nas_filter,
                                               exclude_patterns=exclude_patterns))

    except Exception as e:
        logging.error(f"Error in remove_duplicate_items: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'An error occurred: {str(e)}'
        })

@debug_bp.route('/api/fix_episode_numbers', methods=['POST'])
@admin_required
def fix_episode_numbers():
    """
    Fix incorrect episode numbers in database by parsing from filled_by_file column.

    Finds episodes where episode_number doesn't match the filename and corrects them.
    Also can identify duplicate episodes (same show/season/episode with multiple entries).
    """
    from scraper.functions.ptt_parser import parse_with_ptt
    from database.core import get_db_connection

    dry_run = request.form.get('dry_run') == 'on'
    show_duplicates = request.form.get('show_duplicates') == 'on'
    remove_duplicates = request.form.get('remove_duplicates') == 'on'

    logging.info(f"Episode number fix requested. Dry run: {dry_run}, Show duplicates: {show_duplicates}, Remove duplicates: {remove_duplicates}")

    conn = None
    mismatches = []
    duplicates = []
    torrent_duplicates = []
    fixed_count = 0
    deleted_count = 0

    try:
        conn = get_db_connection()

        # Find episodes - include those with filled_by_file OR location_on_disk/basename
        cursor = conn.execute("""
            SELECT id, title, season_number, episode_number, filled_by_file,
                   location_on_disk, location_basename, filled_by_torrent_id,
                   tmdb_id, imdb_id, collected_at
            FROM media_items
            WHERE type = 'episode'
            AND (
                (filled_by_file IS NOT NULL AND filled_by_file != '')
                OR (location_on_disk IS NOT NULL AND location_on_disk != '')
                OR (location_basename IS NOT NULL AND location_basename != '')
            )
            ORDER BY tmdb_id, season_number, episode_number
        """)

        episodes = cursor.fetchall()
        cursor.close()

        logging.info(f"Scanning {len(episodes)} episodes for incorrect episode numbers")

        # Check each episode
        for ep in episodes:
            ep_id = ep['id']
            db_season = ep['season_number']
            db_episode = ep['episode_number']

            # Get filename with fallback logic
            filename = ep['filled_by_file']
            if not filename:
                # Fallback to location_basename
                filename = ep['location_basename']
            if not filename and ep['location_on_disk']:
                # Fallback to extracting filename from location_on_disk
                import os
                filename = os.path.basename(ep['location_on_disk'])

            if not filename:
                continue  # Skip if no filename available

            try:
                # Parse filename to get actual episode number
                parsed = parse_with_ptt(filename)

                # Safely extract season and episode, handling empty lists
                seasons = parsed.get('seasons') or []
                episodes = parsed.get('episodes') or []
                parsed_season = parsed.get('season') or (seasons[0] if len(seasons) > 0 else None)
                parsed_episode = parsed.get('episode') or (episodes[0] if len(episodes) > 0 else None)

                if parsed_season is None or parsed_episode is None:
                    continue

                parsed_season = int(parsed_season)
                parsed_episode = int(parsed_episode)

                # For multi-episode files (S01E01E02), check if db_episode is in the episode range
                episodes_int = [int(e) for e in episodes] if episodes else [parsed_episode]

                # Check if there's a mismatch
                # Season must match AND episode must be in the parsed episodes list
                if db_season != parsed_season or db_episode not in episodes_int:
                    # Format episode display for multi-episode files
                    if len(episodes_int) > 1:
                        parsed_ep_display = f"{episodes_int[0]}-{episodes_int[-1]}"
                    else:
                        parsed_ep_display = parsed_episode

                    mismatch = {
                        'id': ep_id,
                        'title': ep['title'],
                        'tmdb_id': ep['tmdb_id'],
                        'imdb_id': ep['imdb_id'],
                        'filename': filename,
                        'db_season': db_season,
                        'db_episode': db_episode,
                        'parsed_season': parsed_season,
                        'parsed_episode': parsed_ep_display,
                        'filled_by_torrent_id': ep['filled_by_torrent_id']
                    }
                    mismatches.append(mismatch)

                    logging.info(
                        f"Found mismatch ID {ep_id}: {ep['title']} "
                        f"S{db_season}E{db_episode} -> S{parsed_season}E{parsed_episode} "
                        f"(File: {filename})"
                    )

                    # Fix if not dry run
                    if not dry_run:
                        conn.execute(
                            """UPDATE media_items
                               SET season_number = ?, episode_number = ?
                               WHERE id = ?""",
                            (parsed_season, parsed_episode, ep_id)
                        )
                        fixed_count += 1

            except Exception as parse_err:
                logging.warning(f"Could not parse {filename}: {parse_err}")
                continue

        if not dry_run and fixed_count > 0:
            conn.commit()
            logging.info(f"Fixed {fixed_count} episode numbers")

        # Find torrent ID based duplicates (bug-created duplicates)
        # TRUE TMDB FALLBACK: Prioritize TMDB ID, use IMDb ID only when TMDB is missing
        # Uses COALESCE to create unified identifier: 'tmdb_' + TMDB ID if available, else IMDb ID
        if remove_duplicates or show_duplicates:
            torrent_dup_cursor = conn.execute("""
                SELECT
                    filled_by_torrent_id,
                    COALESCE('tmdb_' || tmdb_id, imdb_id) as unified_id,
                    imdb_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    COUNT(*) as count,
                    GROUP_CONCAT(id || ':' || season_number || 'x' || episode_number || ':' ||
                                 COALESCE(filled_by_file, location_basename, '?'), ' | ') as episodes,
                    GROUP_CONCAT(id) as ids,
                    GROUP_CONCAT(collected_at) as collected_dates
                FROM media_items
                WHERE type = 'episode'
                AND filled_by_torrent_id IS NOT NULL
                AND filled_by_torrent_id != ''
                AND (ghostlisted = 0 OR ghostlisted IS NULL)
                GROUP BY filled_by_torrent_id, COALESCE('tmdb_' || tmdb_id, imdb_id), season_number, episode_number
                HAVING count > 1
                ORDER BY unified_id, season_number, episode_number, filled_by_torrent_id
            """)

            torrent_dupes = torrent_dup_cursor.fetchall()
            torrent_dup_cursor.close()

            for dup in torrent_dupes:
                # These are TRUE duplicates: same torrent + same show + same season + same episode
                # Now we need to figure out which entry is "correct" (matches filename) and which are "wrong"
                ids = dup['ids'].split(',')
                episodes_info = dup['episodes'].split(' | ')
                db_season = dup['season_number']
                db_episode = dup['episode_number']

                correct_entries = []
                wrong_entries = []

                for i, ep_info in enumerate(episodes_info):
                    parts = ep_info.split(':')
                    if len(parts) >= 3:
                        ep_id = parts[0]
                        se_info = parts[1]  # like "1x5"
                        filename = parts[2] if len(parts) > 2 else None

                        if filename and filename != '?':
                            # Parse filename to check if episode number matches database value
                            try:
                                parsed = parse_with_ptt(filename)

                                # Safely extract season and episode, handling empty lists
                                seasons = parsed.get('seasons') or []
                                episodes = parsed.get('episodes') or []
                                parsed_season = parsed.get('season') or (seasons[0] if len(seasons) > 0 else None)
                                parsed_episode = parsed.get('episode') or (episodes[0] if len(episodes) > 0 else None)

                                if parsed_season is not None and parsed_episode is not None:
                                    parsed_s = int(parsed_season)
                                    # For multi-episode files, check if db_episode is in the range
                                    episodes_int = [int(e) for e in episodes] if episodes else [int(parsed_episode)]

                                    # If filename matches the database entry, it's "correct"
                                    # If filename doesn't match, it's a "wrong" duplicate
                                    if parsed_s == db_season and db_episode in episodes_int:
                                        correct_entries.append(ep_id)
                                    else:
                                        wrong_entries.append(ep_id)
                                else:
                                    # Can't parse filename - treat as potentially wrong
                                    wrong_entries.append(ep_id)
                            except:
                                # Parse failed - treat as potentially wrong
                                wrong_entries.append(ep_id)
                        else:
                            # No filename - can't determine, treat as potentially wrong
                            wrong_entries.append(ep_id)

                # Build duplicate info
                # We always report these since they're true duplicates
                torrent_duplicates.append({
                    'torrent_id': dup['filled_by_torrent_id'],
                    'tmdb_id': dup['tmdb_id'],
                    'season': db_season,
                    'episode': db_episode,
                    'total_count': dup['count'],
                    'correct_ids': correct_entries,
                    'wrong_ids': wrong_entries,
                    'episodes_info': f"S{db_season}E{db_episode}"
                })

                # Delete wrong entries if remove_duplicates is enabled
                if remove_duplicates and not dry_run and wrong_entries:
                    for wrong_id in wrong_entries:
                        conn.execute("DELETE FROM media_items WHERE id = ?", (int(wrong_id),))
                        deleted_count += 1
                        logging.info(f"Deleted duplicate entry ID {wrong_id} (duplicate S{db_season}E{db_episode}, torrent {dup['filled_by_torrent_id']})")

        if not dry_run and deleted_count > 0:
            conn.commit()
            logging.info(f"Deleted {deleted_count} duplicate entries")

        # Find duplicate episodes if requested
        # TRUE TMDB FALLBACK: Prioritize TMDB ID, use IMDb ID only when TMDB is missing
        # Uses COALESCE to create unified identifier: 'tmdb_' + TMDB ID if available, else IMDb ID
        if show_duplicates:
            dup_cursor = conn.execute("""
                SELECT
                    COALESCE('tmdb_' || tmdb_id, imdb_id) as unified_id,
                    imdb_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    COUNT(*) as count,
                    GROUP_CONCAT(id) as ids,
                    GROUP_CONCAT(filled_by_file, ' | ') as files,
                    GROUP_CONCAT(filled_by_torrent_id, ' | ') as torrent_ids
                FROM media_items
                WHERE type = 'episode'
                AND (ghostlisted = 0 OR ghostlisted IS NULL)
                GROUP BY COALESCE('tmdb_' || tmdb_id, imdb_id), season_number, episode_number
                HAVING count > 1
                ORDER BY unified_id, season_number, episode_number
            """)

            duplicates = [dict(row) for row in dup_cursor.fetchall()]
            dup_cursor.close()

            logging.info(f"Found {len(duplicates)} episodes with multiple entries")

        conn.close()

        return jsonify({
            'success': True,
            'dry_run': dry_run,
            'mismatches_found': len(mismatches),
            'mismatches': mismatches,  # Return all for pagination
            'fixed_count': fixed_count if not dry_run else 0,
            'duplicates_found': len(duplicates) if show_duplicates else None,
            'duplicates': duplicates if show_duplicates else None,  # Return all for pagination
            'torrent_duplicates_found': len(torrent_duplicates),
            'torrent_duplicates': torrent_duplicates,  # Return all for pagination
            'deleted_count': deleted_count if not dry_run else 0
        })

    except Exception as e:
        if conn:
            conn.close()
        logging.error(f"Error in fix_episode_numbers: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'An error occurred: {str(e)}'
        })

@debug_bp.route('/api/cleanup_collected_watchlist', methods=['POST'])
@admin_required
def cleanup_collected_watchlist():
    """Remove all Collected items from My Plex Watchlist and all Other Plex Watchlists."""
    import threading
    from flask import current_app
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                from content_checkers.plex_watchlist import get_plex_client
                from database.core import get_db_connection
                from plexapi.myplex import MyPlexAccount

                # Build set of collected imdb_ids
                conn = get_db_connection()
                rows = conn.execute(
                    "SELECT DISTINCT imdb_id, title, type FROM media_items WHERE state = 'Collected' AND imdb_id IS NOT NULL AND imdb_id != ''"
                ).fetchall()
                conn.close()

                collected = {row['imdb_id']: {'imdb_id': row['imdb_id'], 'title': row['title'], 'type': row['type']} for row in rows}
                if not collected:
                    logging.info("[WatchlistCleanup] No collected items with IMDB IDs found.")
                    return

                logging.info(f"[WatchlistCleanup] Found {len(collected)} collected items to check against watchlists.")
                total_removed = 0

                def _extract_imdb(witem):
                    for guid in getattr(witem, 'guids', []):
                        guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                        if 'imdb://' in guid_str:
                            return guid_str.replace('imdb://', '').strip()
                    return None

                # --- My Plex Watchlist ---
                try:
                    account, _token = get_plex_client()
                    if account:
                        watchlist = account.watchlist()
                        logging.info(f"[WatchlistCleanup] My Plex Watchlist has {len(watchlist)} items.")
                        for witem in watchlist:
                            witem_imdb = _extract_imdb(witem)
                            if witem_imdb and witem_imdb in collected:
                                try:
                                    account.removeFromWatchlist(witem)
                                    total_removed += 1
                                    logging.info(f"[WatchlistCleanup] Removed '{collected[witem_imdb]['title']}' ({witem_imdb}) from My Plex Watchlist.")
                                except Exception as e:
                                    logging.warning(f"[WatchlistCleanup] Failed to remove {witem_imdb} from My Plex Watchlist: {e}")
                except Exception as e:
                    logging.error(f"[WatchlistCleanup] Error accessing My Plex Watchlist: {e}")

                # --- Other Plex Watchlists ---
                try:
                    runner = get_program_runner()
                    content_sources = runner.get_content_sources() if runner else {}
                    for source_key, source_data in content_sources.items():
                        if source_data.get('type') != 'Other Plex Watchlist':
                            continue
                        username = source_data.get('username', '')
                        other_token = source_data.get('token', '')
                        if not other_token:
                            continue
                        try:
                            other_account = MyPlexAccount(token=other_token)
                            watchlist = other_account.watchlist()
                            logging.info(f"[WatchlistCleanup] {username}'s watchlist has {len(watchlist)} items.")
                            for witem in watchlist:
                                witem_imdb = _extract_imdb(witem)
                                if witem_imdb and witem_imdb in collected:
                                    try:
                                        other_account.removeFromWatchlist(witem)
                                        total_removed += 1
                                        logging.info(f"[WatchlistCleanup] Removed '{collected[witem_imdb]['title']}' ({witem_imdb}) from {username}'s Plex Watchlist.")
                                    except Exception as e:
                                        logging.warning(f"[WatchlistCleanup] Failed to remove {witem_imdb} from {username}'s watchlist: {e}")
                        except Exception as e:
                            logging.error(f"[WatchlistCleanup] Error accessing {username}'s Plex Watchlist: {e}")
                except Exception as e:
                    logging.error(f"[WatchlistCleanup] Error iterating Other Plex Watchlist sources: {e}")

                logging.info(f"[WatchlistCleanup] Done. Removed {total_removed} items total across all watchlists.")

            except Exception as e:
                logging.error(f"[WatchlistCleanup] Unexpected error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'message': 'Watchlist cleanup started — check logs for progress.'})
