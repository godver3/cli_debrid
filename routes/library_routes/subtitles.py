"""Subtitle downloads for an item, season or show."""

from flask import jsonify, request
import logging
from ._common import library_bp

def _collected(where, args):
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM media_items WHERE state IN ('Collected', 'Upgrading') AND " + where,
            args).fetchall()]
    finally:
        conn.close()


def _run_items(items):
    from utilities.downsub import download_subtitles_for_video, resolve_item_path
    found = 0
    for item in items:
        path = resolve_item_path(item)
        if path and download_subtitles_for_video(path, item=item):
            found += 1
    return found


@library_bp.route('/download_subtitles/item/<int:item_id>', methods=['POST'])
def download_subtitles_item(item_id):
    """Download subtitles for a single movie or episode."""
    try:
        rows = _collected('id = ?', (item_id,))
        if not rows:
            return jsonify({'success': False, 'error': 'Item not found or not collected'}), 404
        if not rows[0].get('location_on_disk'):
            return jsonify({'success': False, 'error': 'No file path available'}), 400
        if _run_items(rows):
            return jsonify({'success': True, 'message': f'Subtitles downloaded for {rows[0]["title"]}'})
        return jsonify({'success': False, 'message': f'No subtitles found for {rows[0]["title"]}'})
    except Exception as e:
        logging.error(f"[DownSub] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _start_bulk(rows, label):
    import threading
    rows = [r for r in rows if r.get('location_on_disk')]
    if not rows:
        return jsonify({'success': False, 'error': 'No collected episodes found'}), 404

    def _run():
        found = _run_items(rows)
        logging.info(f"[DownSub] {label} done: {found}/{len(rows)} subtitles downloaded")
    threading.Thread(target=_run, daemon=True, name='subs-bulk').start()
    return jsonify({'success': True, 'message': f'Searching subtitles for {len(rows)} episodes (see Tools → Subtitles for results)'})


@library_bp.route('/download_subtitles/season', methods=['POST'])
def download_subtitles_season():
    """Download subtitles for all episodes in a season."""
    try:
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        season_number = data.get('season_number')
        if not imdb_id or season_number is None:
            return jsonify({'success': False, 'error': 'imdb_id and season_number required'}), 400
        return _start_bulk(_collected("imdb_id = ? AND season_number = ? AND type = 'episode'",
                                      (imdb_id, season_number)), 'Season')
    except Exception as e:
        logging.error(f"[DownSub] Season error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/download_subtitles/show', methods=['POST'])
def download_subtitles_show():
    """Download subtitles for all collected episodes in a show."""
    try:
        data = request.get_json() or {}
        imdb_id = data.get('imdb_id')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'imdb_id required'}), 400
        return _start_bulk(_collected("imdb_id = ? AND type = 'episode'", (imdb_id,)), 'Show')
    except Exception as e:
        logging.error(f"[DownSub] Show error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
