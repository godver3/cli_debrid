"""cli_mount providers, cleanup jobs, backups/restore and NZB name backfill."""

from flask import jsonify, request
import logging
from routes import admin_required
from routes.models import user_required
import os
from datetime import datetime
from ._common import debug_bp, scan_progress

# ---------------------------------------------------------------------------
# cli_mount Cleanup
# ---------------------------------------------------------------------------

@debug_bp.route('/api/climount_providers', methods=['GET'])
@admin_required
def climount_providers():
    """Return list of debrid provider names configured in cli_mount."""
    try:
        from utilities.settings import get_setting
        from utilities.climount_cleanup import get_climount_providers
        data_path = get_setting('Usenet Provider', 'data_path', '/climount_data')
        db_dir = os.path.join(data_path, 'db')
        providers = get_climount_providers(db_dir)
        return jsonify({'success': True, 'providers': providers, 'data_path': data_path})
    except Exception as e:
        logging.error(f"climount_providers error: {e}")
        return jsonify({'success': False, 'error': str(e), 'providers': []})

_climount_cleanup_jobs = {}  # job_id -> result dict

@debug_bp.route('/api/climount_cleanup', methods=['POST'])
@admin_required
def climount_cleanup():
    """Run cli_mount cleanup in a background thread. Returns job_id immediately."""
    import uuid, threading
    try:
        data = request.get_json() or {}
        provider = data.get('provider', '').strip()
        dry_run = bool(data.get('dry_run', True))
        db_path = data.get('db_path', '').strip()

        if not provider:
            return jsonify({'success': False, 'error': 'Provider is required'}), 400
        if not db_path:
            from utilities.settings import get_setting
            base = get_setting('Usenet Provider', 'data_path', '/climount_data')
            db_path = os.path.join(base, 'db')
        if not os.path.isdir(db_path):
            return jsonify({'success': False, 'error': f'DB directory not found: {db_path}'}), 400

        job_id = str(uuid.uuid4())[:8]
        _climount_cleanup_jobs[job_id] = {'status': 'running', 'result': None}

        def _run():
            try:
                from utilities.climount_cleanup import run_cleanup
                result = run_cleanup(db_path, provider, dry_run)
                _climount_cleanup_jobs[job_id] = {'status': 'done', 'result': result}
            except Exception as e:
                logging.error(f"cli_mount cleanup job {job_id} error: {e}", exc_info=True)
                _climount_cleanup_jobs[job_id] = {
                    'status': 'error',
                    'result': {'success': False, 'error': str(e), 'lines': [f"ERROR: {e}"]}
                }

        threading.Thread(target=_run, daemon=True, name=f'climount-cleanup-{job_id}').start()
        return jsonify({'success': True, 'job_id': job_id})

    except Exception as e:
        logging.error(f"climount_cleanup error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/climount_cleanup_status/<job_id>', methods=['GET'])
@admin_required
def climount_cleanup_status(job_id):
    job = _climount_cleanup_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(job)

# ---------------------------------------------------------------------------
# cli_mount DB Backup / Restore / Scan / Delete
# (follows same pattern as CLI DB equivalents)
# ---------------------------------------------------------------------------

def _get_dcy_dirs():
    """Return (data_path, db_dir, backup_dir) or raise if not configured."""
    from utilities.settings import get_setting
    data_path = get_setting('Usenet Provider', 'data_path', '').strip()
    if not data_path:
        raise ValueError("cli_mount Data Path is not configured in Settings → Usenet Provider")
    db_dir = os.path.join(data_path, 'db')
    backup_dir = os.path.join(db_dir, 'backups')
    return data_path, db_dir, backup_dir

def _dcy_is_valid_hybr(path):
    """Check if file starts with HYBR magic bytes."""
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'HYBR'
    except Exception:
        return False

def _dcy_age_display(age_sec):
    if age_sec < 3600: return f"{int(age_sec/60)}m ago"
    if age_sec < 86400: return f"{int(age_sec/3600)}h ago"
    return f"{int(age_sec/86400)}d ago"

@debug_bp.route('/api/climount_backup_now', methods=['POST'])
@admin_required
def climount_backup_now():
    """Trigger an immediate cli_mount DB backup."""
    try:
        from main import backup_climount_databases
        ok = backup_climount_databases()
        if ok:
            return jsonify({'success': True, 'message': 'cli_mount databases backed up successfully'})
        return jsonify({'success': False, 'error': 'Backup failed — check logs'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/list_climount_backups', methods=['GET'])
@admin_required
def list_climount_backups():
    """List available cli_mount DB backups — same structure as list_database_backups."""
    try:
        _, db_dir, backup_dir = _get_dcy_dirs()
        if not os.path.isdir(backup_dir):
            return jsonify({'success': True, 'backups': []})

        import time as _time
        now = _time.time()
        backups = []
        for fname in sorted(os.listdir(backup_dir)):
            fpath = os.path.join(backup_dir, fname)
            if not fname.endswith('.db'):
                continue
            stat = os.stat(fpath)
            age = now - stat.st_mtime
            is_valid = _dcy_is_valid_hybr(fpath)
            # Category
            if fname.startswith(('entries_pre_restore_', 'items_pre_restore_')):
                cat = 'safety_backup'
            elif not is_valid:
                cat = 'corrupted'
            elif age > 7 * 86400:
                cat = 'old_version'
            else:
                cat = 'recent_backup'
            backups.append({
                'filename': fname,
                'path': fpath,
                'size_mb': round(stat.st_size / (1024*1024), 2),
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'age_seconds': int(age),
                'age_display': _dcy_age_display(age),
                'category': cat,
                'is_valid': is_valid,
            })
        backups.sort(key=lambda x: x['age_seconds'])
        return jsonify({'success': True, 'backups': backups})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/request_climount_restore', methods=['POST'])
@admin_required
def request_climount_restore():
    """
    Restore a cli_mount DB file by direct copy (no container restart needed —
    cli_mount must be stopped by the user first).
    """
    try:
        data = request.get_json() or {}
        backup_path = data.get('backup_path', '').strip()
        create_safety = bool(data.get('create_safety_backup', True))
        _, db_dir, backup_dir = _get_dcy_dirs()

        if not backup_path:
            return jsonify({'success': False, 'error': 'backup_path required'}), 400
        if not os.path.exists(backup_path):
            return jsonify({'success': False, 'error': f'Backup file not found: {backup_path}'}), 400
        if not _dcy_is_valid_hybr(backup_path):
            return jsonify({'success': False, 'error': 'Backup file is not a valid HYBR database'}), 400

        # Determine target filename (entries.db or items.db) from backup name
        fname = os.path.basename(backup_path)
        if fname.startswith('entries'):
            target = os.path.join(db_dir, 'entries.db')
        elif fname.startswith('items'):
            target = os.path.join(db_dir, 'items.db')
        else:
            return jsonify({'success': False, 'error': f'Cannot determine target DB from filename: {fname}'}), 400

        import shutil as _shutil
        # Create safety backup if requested
        if create_safety and os.path.exists(target):
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            safety = os.path.join(backup_dir, f'{os.path.splitext(os.path.basename(target))[0]}_pre_restore_{ts}.db')
            os.makedirs(backup_dir, exist_ok=True)
            _shutil.copy2(target, safety)
            logging.info(f"[DCY_RESTORE] Safety backup: {safety}")

        _shutil.copy2(backup_path, target)
        logging.info(f"[DCY_RESTORE] Restored {fname} → {target}")
        return jsonify({'success': True, 'message': f'Restored {os.path.basename(target)} from {fname}. Start cli_mount to apply.'})

    except Exception as e:
        logging.error(f"[DCY_RESTORE] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/scan_climount_old_databases', methods=['GET'])
@admin_required
def scan_climount_old_databases():
    """Scan cli_mount backup dir for old/corrupted DB files — mirrors scan_old_databases."""
    try:
        _, db_dir, backup_dir = _get_dcy_dirs()
        if not os.path.isdir(backup_dir):
            return jsonify({'success': True, 'files': [], 'total_size_mb': 0})

        import time as _time
        now = _time.time()
        files = []
        protected = {'entries.db', 'items.db'}

        for fname in os.listdir(backup_dir):
            fpath = os.path.join(backup_dir, fname)
            if fname in protected or not fname.endswith('.db'):
                continue
            stat = os.stat(fpath)
            age = now - stat.st_mtime
            is_valid = _dcy_is_valid_hybr(fpath)
            if fname.startswith(('entries_pre_restore_', 'items_pre_restore_')):
                cat = 'safety_backup'
            elif not is_valid:
                cat = 'corrupted'
            elif age > 7 * 86400:
                cat = 'old_version'
            else:
                cat = 'recent_backup'
            files.append({
                'filename': fname,
                'path': fpath,
                'size_mb': round(stat.st_size / (1024*1024), 2),
                'age_display': _dcy_age_display(age),
                'category': cat,
                'is_valid': is_valid,
            })

        total_mb = round(sum(f['size_mb'] for f in files), 2)
        return jsonify({'success': True, 'files': files, 'total_size_mb': total_mb})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/delete_climount_old_databases', methods=['POST'])
@admin_required
def delete_climount_old_databases():
    """Delete selected cli_mount backup files — mirrors delete_old_databases."""
    try:
        data = request.get_json() or {}
        file_paths = data.get('file_paths', [])
        dry_run = bool(data.get('dry_run', False))
        _, db_dir, backup_dir = _get_dcy_dirs()

        protected_names = {'entries.db', 'items.db'}
        deleted = []
        skipped = []
        freed_mb = 0.0

        for fpath in file_paths:
            fname = os.path.basename(fpath)
            if fname in protected_names:
                skipped.append({'file': fname, 'reason': 'Protected system file'})
                continue
            if not fpath.startswith(backup_dir):
                skipped.append({'file': fname, 'reason': 'Outside backup directory'})
                continue
            if not os.path.exists(fpath):
                skipped.append({'file': fname, 'reason': 'File not found'})
                continue
            size_mb = os.path.getsize(fpath) / (1024*1024)
            if not dry_run:
                os.remove(fpath)
                freed_mb += size_mb
                logging.info(f"[DCY_CLEANUP] Deleted: {fname}")
            else:
                freed_mb += size_mb
            deleted.append(fname)

        return jsonify({
            'success': True,
            'deleted': deleted,
            'skipped': skipped,
            'deleted_count': len(deleted),
            'skipped_count': len(skipped),
            'total_size_freed_mb': round(freed_mb, 2),
            'dry_run': dry_run,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/backfill_nzb_names', methods=['POST'])
@user_required
def backfill_nzb_names():
    """Backfill NZB job names for collected NZB items.
    Tier 1: Items not yet in NZB format — rename to new format.
    Tier 2: Items already in NZB format — update tags, version, content source.
    """
    import threading, uuid as _uuid
    from utilities.settings import get_setting as _gs
    task_id = str(_uuid.uuid4())

    if not _gs('Usenet Provider', 'enable_nzb_naming', False):
        return jsonify({'success': False, 'error': 'NZB file naming is not enabled in settings'}), 400

    data = request.get_json(silent=True) or {}
    item_ids = data.get('item_ids')  # optional list of DB item IDs to limit scope
    force = data.get('force', False)  # if True, re-process even if already named
    dry_run = data.get('dry_run', False)  # if True, preview only — no DB or cli_mount changes

    def _run():
        try:
            import os as _os
            from database.core import get_db_connection
            from routes.scraper_routes import _build_nzb_title, _get_content_source_display_name
            from usenet.climount_client import get_climount_client
            client = get_climount_client()

            # Backup both DBs before making any changes (non-dry-run only)
            if not dry_run:
                try:
                    from main import backup_database
                    backup_database()
                    logging.info("[NZBBackfill] CLI DB backup completed.")
                except Exception as _be:
                    logging.warning(f"[NZBBackfill] CLI DB backup failed: {_be}")
                try:
                    from main import backup_climount_databases
                    backup_climount_databases()
                    logging.info("[NZBBackfill] cli_mount DB backup completed.")
                except Exception as _be:
                    logging.warning(f"[NZBBackfill] cli_mount DB backup failed: {_be}")

            def _build_location(media_type, new_folder, ext):
                """Build correct location_on_disk.
                Both folder and filename use new_folder (new NZB name).
                /debrid/movies/NewName/NewName.mkv
                """
                folder = 'movies' if media_type != 'episode' else 'shows'
                return f'/debrid/{folder}/{new_folder}/{new_folder}{ext}'

            conn = get_db_connection()
            cursor = conn.cursor()
            if item_ids:
                placeholders = ','.join('?' * len(item_ids))
                cursor.execute(f"""
                    SELECT id, title, year, imdb_id, tmdb_id, type, season_number, episode_number,
                           episode_title, version, original_scraped_torrent_title, filled_by_title,
                           filled_by_torrent_id, location_on_disk, debrid_folder_name,
                           content_source, tags, filled_by_file, location_basename
                    FROM media_items
                    WHERE id IN ({placeholders})
                    AND state = 'Collected'
                    AND filled_by_torrent_id IS NOT NULL AND filled_by_torrent_id != ''
                """, item_ids)
            else:
                cursor.execute("""
                    SELECT id, title, year, imdb_id, tmdb_id, type, season_number, episode_number,
                           episode_title, version, original_scraped_torrent_title, filled_by_title,
                           filled_by_torrent_id, location_on_disk, debrid_folder_name,
                           content_source, tags, filled_by_file, location_basename
                    FROM media_items
                    WHERE state = 'Collected'
                    AND filled_by_torrent_id IS NOT NULL AND filled_by_torrent_id != ''
                    ORDER BY collected_at DESC
                """)
            rows = cursor.fetchall()
            conn.close()

            # Group by filled_by_torrent_id to detect season packs
            # Season pack = multiple episodes sharing the same torrent ID
            from collections import defaultdict
            torrent_groups = defaultdict(list)
            for row in rows:
                torrent_groups[row['filled_by_torrent_id']].append(dict(row))

            # Pre-compute season folder names for season packs
            # season_folder_map: torrent_id -> season_level_folder_name
            season_folder_map = {}
            for torrent_id, group in torrent_groups.items():
                if len(group) > 1 and group[0]['type'] == 'episode':
                    # Season pack — build season-level name using first item
                    first = group[0]
                    fbf0 = first.get('filled_by_file') or ''
                    fbf0_noext = _os.path.splitext(fbf0)[0]
                    fbf0_is_nzb = '{imdb-' in fbf0
                    orig0 = (
                        (fbf0_noext if '.' in fbf0_noext and not fbf0_is_nzb else None) or
                        first.get('original_scraped_torrent_title') or
                        None
                    )
                    cs0 = _get_content_source_display_name(first.get('content_source'))
                    season_folder = _build_nzb_title(
                        title=first.get('title'),
                        year=first.get('year'),
                        imdb_id=first.get('imdb_id'),
                        version=first.get('version'),
                        original_scraped_torrent_title=orig0,
                        media_type='show',
                        season=first.get('season_number'),
                        episode=None,
                        episode_title=None,
                        tags=first.get('tags') or None,
                        content_source_display_name=cs0,
                    )
                    if season_folder:
                        season_folder_map[torrent_id] = season_folder

            # Track which torrent IDs have already been renamed in cli_mount
            climount_renamed = set()
            # Queue of {infohash: new_name} to batch rename in cli_mount after loop
            dcy_rename_queue = {}

            scan_progress[task_id].update({
                'status': 'running',
                'total': len(rows),
                'processed': 0,
                'renamed': 0,
                'skipped': 0,
                'errors': 0,
                'message': f'Processing {len(rows)} items...',
            })

            renamed = 0
            skipped = 0
            errors = 0

            for i, row in enumerate(rows):
                item = dict(row)
                try:
                    # filled_by_file is the original release filename e.g. "Release.Name.mkv"
                    fbf = item.get('filled_by_file') or ''
                    fbf_noext, fbf_ext = _os.path.splitext(fbf)

                    # location_basename is the most reliable source of the original filename
                    loc_basename = item.get('location_basename') or ''
                    loc_basename_noext = _os.path.splitext(loc_basename)[0] if loc_basename else ''

                    # Check if filled_by_file was corrupted (overwritten with NZB format)
                    fbf_is_nzb_format = '{imdb-' in fbf

                    # If filled_by_file is corrupted, use location_basename if it's clean
                    if fbf_is_nzb_format and loc_basename and '{imdb-' not in loc_basename:
                        fbf = loc_basename
                        fbf_noext, fbf_ext = _os.path.splitext(fbf)
                        fbf_is_nzb_format = False

                    # original_scraped_torrent_title priority:
                    # 1. filled_by_file (no ext) if it has dots (reliable release name format)
                    # 2. location_basename (no ext) if clean
                    # 3. existing original_scraped_torrent_title
                    fbf_has_dots = '.' in fbf_noext and not fbf_is_nzb_format
                    existing_orig = item.get('original_scraped_torrent_title') or ''
                    existing_orig_clean = existing_orig if '{imdb-' not in existing_orig else None
                    orig_scraped = (
                        (fbf_noext if fbf_has_dots else None) or
                        (loc_basename_noext if loc_basename and '{imdb-' not in loc_basename else None) or
                        existing_orig_clean or
                        None
                    )

                    cs_display = _get_content_source_display_name(item.get('content_source'))
                    media_type = 'episode' if item.get('type') == 'episode' else 'movie'
                    logging.info(f"[NZBBackfill] item={item['id']} orig_scraped={orig_scraped!r} fbf={fbf!r} fbf_noext={fbf_noext!r}")
                    new_name = _build_nzb_title(
                        title=item.get('title'),
                        year=item.get('year'),
                        imdb_id=item.get('imdb_id'),
                        version=item.get('version'),
                        original_scraped_torrent_title=orig_scraped,
                        media_type=media_type,
                        season=item.get('season_number'),
                        episode=item.get('episode_number'),
                        episode_title=item.get('episode_title'),
                        tags=item.get('tags') or None,
                        content_source_display_name=cs_display,
                    )

                    current_name = item.get('filled_by_title') or item.get('debrid_folder_name') or ''

                    if not new_name or (new_name == current_name and not force):
                        skipped += 1
                        continue

                    # Detect season pack
                    torrent_id = item.get('filled_by_torrent_id') or ''
                    is_season_pack = torrent_id in season_folder_map
                    season_folder = season_folder_map.get(torrent_id)

                    # Build location_on_disk
                    # For filename, prefer filled_by_file, fall back to location_basename
                    loc_filename = fbf or loc_basename or ''
                    ext = fbf_ext if fbf_ext else (_os.path.splitext(loc_basename)[1] if loc_basename else '.mkv')
                    if is_season_pack and season_folder and loc_filename:
                        # Season pack: folder = season name, filename = original file (filled_by_file or location_basename)
                        new_loc = f'/debrid/shows/{season_folder}/{loc_filename}'
                        dcy_rename_name = season_folder
                    else:
                        # Single episode or movie: folder and filename both = new_name
                        new_loc = _build_location(media_type, new_name, ext)
                        dcy_rename_name = new_name

                    if dry_run:
                        renamed += 1
                        logging.info(f"[NZBBackfill][DRY] {item['id']} ({item.get('title')}): -> {new_name!r} dcy={dcy_rename_name!r} loc={new_loc!r} {'[PACK]' if is_season_pack else ''}")
                        scan_progress[task_id].update({'processed': i + 1, 'renamed': renamed, 'skipped': skipped, 'errors': errors, 'message': f"[DRY] {item.get('title', '')}"})
                        continue

                    # Collect cli_mount renames — deduplicated, batched after loop
                    if torrent_id.startswith('nzb:') and torrent_id not in climount_renamed:
                        climount_renamed.add(torrent_id)
                        dcy_rename_queue[torrent_id[4:]] = dcy_rename_name

                    conn2 = get_db_connection()
                    try:
                        conn2.execute("""
                            UPDATE media_items
                            SET filled_by_title = ?,
                                debrid_folder_name = ?,
                                location_on_disk = ?,
                                last_updated = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (new_name, dcy_rename_name, new_loc, item['id']))
                        conn2.commit()
                    finally:
                        conn2.close()

                    renamed += 1
                    logging.info(f"[NZBBackfill] {item['id']} ({item.get('title')}): -> {new_name!r} loc={new_loc!r} {'[PACK]' if is_season_pack else ''}")

                except Exception as e:
                    logging.error(f"[NZBBackfill] Error on item {item.get('id')}: {e}")
                    errors += 1

                scan_progress[task_id].update({
                    'processed': i + 1,
                    'renamed': renamed,
                    'skipped': skipped,
                    'errors': errors,
                    'message': f"Processing: {item.get('title', '')}",
                })

            # Batch rename in cli_mount using thread pool (10 workers)
            if dcy_rename_queue and not dry_run:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                total_dcy = len(dcy_rename_queue)
                dcy_done = 0
                scan_progress[task_id].update({'message': f'DB updated={renamed}. Sending {total_dcy} renames to cli_mount...'})
                logging.info(f"[NZBBackfill] Sending {total_dcy} unique renames to cli_mount (10 workers)...")

                def _do_rename(args):
                    h, name = args
                    return client.rename_nzb(h, name)

                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(_do_rename, item): item for item in dcy_rename_queue.items()}
                    for future in as_completed(futures):
                        dcy_done += 1
                        if dcy_done % 100 == 0:
                            scan_progress[task_id].update({'message': f'cli_mount renames: {dcy_done}/{total_dcy}...'})

                logging.info(f"[NZBBackfill] cli_mount renames complete: {dcy_done}/{total_dcy}")

            scan_progress[task_id].update({
                'status': 'complete',
                'complete': True,
                'message': f'Done. DB updated={renamed} Skipped={skipped} Errors={errors}' + ('' if dry_run else f' | cli_mount: {len(dcy_rename_queue)} entries renamed.'),
            })

        except Exception as e:
            logging.error(f"[NZBBackfill] Fatal: {e}", exc_info=True)
            scan_progress[task_id].update({'status': 'error', 'complete': True, 'message': str(e)})

    scan_progress[task_id] = {'status': 'starting', 'complete': False}
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})

@debug_bp.route('/api/backfill_nzb_names/status/<task_id>', methods=['GET'])
@user_required
def backfill_nzb_names_status(task_id):
    """Get status of a backfill_nzb_names task."""
    return jsonify(scan_progress.get(task_id, {'status': 'not_found', 'complete': True}))
