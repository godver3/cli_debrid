"""Subtitle downloads for an item, season or show."""

from flask import jsonify, request
import logging
from ._common import library_bp

@library_bp.route('/download_subtitles/item/<int:item_id>', methods=['POST'])
def download_subtitles_item(item_id):
    """Download subtitles for a single movie or episode."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting
        conn = get_db_connection()
        row = conn.execute(
            'SELECT id, title, type, location_on_disk, filled_by_file FROM media_items WHERE id = ? AND state IN ("Collected", "Upgrading")',
            (item_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Item not found or not collected'}), 404

        location = row['location_on_disk'] or ''
        filled_by_file = row['filled_by_file'] or ''
        if not location or not filled_by_file:
            return jsonify({'success': False, 'error': 'No file path available'}), 400

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        if is_plex:
            from utilities.downsub import download_subtitles_for_video
            import os
            # Remap /debrid/ to actual mount path
            from utilities.settings import load_config
            import json as _json
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            mount_base = ''
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass
            if mount_base:
                parts = location.split('/', 3)
                if len(parts) >= 3 and parts[1] == 'debrid':
                    location = mount_base + '/' + '/'.join(parts[2:])
            full_path = os.path.join(location, filled_by_file) if not location.endswith(filled_by_file) else location
        else:
            full_path = location

        from utilities.downsub import download_subtitles_for_video
        found = download_subtitles_for_video(full_path)
        if found:
            return jsonify({'success': True, 'message': f'Subtitles downloaded for {row["title"]}'})
        else:
            return jsonify({'success': False, 'message': f'No subtitles found for {row["title"]}'})
    except Exception as e:
        logging.error(f"[DownSub] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/download_subtitles/season', methods=['POST'])
def download_subtitles_season():
    """Download subtitles for all episodes in a season."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting, load_config
        import os, json as _json, threading
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        season_number = data.get('season_number')
        if not imdb_id or season_number is None:
            return jsonify({'success': False, 'error': 'imdb_id and season_number required'}), 400

        conn = get_db_connection()
        rows = conn.execute(
            'SELECT location_on_disk, filled_by_file FROM media_items WHERE imdb_id = ? AND season_number = ? AND state IN ("Collected", "Upgrading") AND type = "episode"',
            (imdb_id, season_number)
        ).fetchall()
        conn.close()

        if not rows:
            return jsonify({'success': False, 'error': 'No collected episodes found'}), 404

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        mount_base = ''
        if is_plex:
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass

        paths = []
        for row in rows:
            loc = row['location_on_disk'] or ''
            fbf = row['filled_by_file'] or ''
            if not loc or not fbf:
                continue
            if is_plex:
                if mount_base:
                    parts = loc.split('/', 3)
                    if len(parts) >= 3 and parts[1] == 'debrid':
                        loc = mount_base + '/' + '/'.join(parts[2:])
                paths.append(os.path.join(loc, fbf) if not loc.endswith(fbf) else loc)
            else:
                # Symlink mode: location_on_disk is already the full path to the symlink file
                paths.append(loc)

        def _run():
            from utilities.downsub import download_subtitles_for_video
            found = sum(1 for p in paths if download_subtitles_for_video(p))
            logging.info(f"[DownSub] Season done: {found}/{len(paths)} subtitles downloaded")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'success': True, 'message': f'Searching subtitles for {len(paths)} episodes (check logs for results)'})
    except Exception as e:
        logging.error(f"[DownSub] Season error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/download_subtitles/show', methods=['POST'])
def download_subtitles_show():
    """Download subtitles for all collected episodes in a show."""
    try:
        from database.core import get_db_connection
        from utilities.settings import get_setting, load_config
        import os, json as _json, threading
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'imdb_id required'}), 400

        conn = get_db_connection()
        rows = conn.execute(
            'SELECT location_on_disk, filled_by_file FROM media_items WHERE imdb_id = ? AND state IN ("Collected", "Upgrading") AND type = "episode"',
            (imdb_id,)
        ).fetchall()
        conn.close()

        if not rows:
            return jsonify({'success': False, 'error': 'No collected episodes found'}), 404

        is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
        mount_base = ''
        if is_plex:
            cfg = load_config()
            data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
            if data_path:
                try:
                    with open(os.path.join(data_path, 'config.json')) as f:
                        dc_cfg = _json.load(f)
                    mount_base = (dc_cfg.get('mount', {}).get('mount_path') or '').rstrip('/')
                except Exception:
                    pass

        paths = []
        for row in rows:
            loc = row['location_on_disk'] or ''
            fbf = row['filled_by_file'] or ''
            if not loc or not fbf:
                continue
            if is_plex:
                if mount_base:
                    parts = loc.split('/', 3)
                    if len(parts) >= 3 and parts[1] == 'debrid':
                        loc = mount_base + '/' + '/'.join(parts[2:])
                paths.append(os.path.join(loc, fbf) if not loc.endswith(fbf) else loc)
            else:
                # Symlink mode: location_on_disk is already the full path to the symlink file
                paths.append(loc)

        def _run():
            from utilities.downsub import download_subtitles_for_video
            found = sum(1 for p in paths if download_subtitles_for_video(p))
            logging.info(f"[DownSub] Show done: {found}/{len(paths)} subtitles downloaded")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'success': True, 'message': f'Searching subtitles for {len(paths)} episodes (check logs for results)'})
    except Exception as e:
        logging.error(f"[DownSub] Show error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
