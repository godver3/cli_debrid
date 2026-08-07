import logging
import threading
from flask import Blueprint, render_template, jsonify, request  # request used by queue_items
from .models import admin_required
from utilities.settings import get_setting, set_setting

upgrade_hub_bp = Blueprint('upgrade_hub', __name__)


def _build_nw_filter():
    """Build a filter function that returns True if a candidate should be hidden."""
    from database.not_wanted_magnets import load_not_wanted_magnets, get_base_filename, normalize_title as _nt
    _nw_all    = load_not_wanted_magnets()
    # Magnet hashes: extract from magnet URIs and plain 40/32-char hash strings
    _nw_hashes = {get_base_filename(m) for m in _nw_all
                  if m and (m.startswith('magnet:') or len(m) in (32, 40))}
    # Title strings: everything else (already normalized when stored)
    _nw_titles = {m for m in _nw_all
                  if m and not m.startswith('magnet:') and len(m) not in (32, 40)}
    def _nw(c):
        t = c.get('new_title') or ''
        if t.lower().startswith('www'):
            return True
        if get_base_filename(c.get('new_magnet', '')) in _nw_hashes:
            return True
        if _nt(t) in _nw_titles:
            return True
        return False
    return _nw


@upgrade_hub_bp.route('/')
@admin_required
def index():
    return render_template('upgrade_hub.html')


# ---------------------------------------------------------------------------
# Scan status / trigger
# ---------------------------------------------------------------------------

@upgrade_hub_bp.route('/api/status')
@admin_required
def status():
    from database.zilean_upgrade import get_scan_status, get_last_results, get_zilean_config
    from database.nzb_upgrade import get_newznab_scrapers
    s = get_scan_status()
    s['zilean_configured'] = get_zilean_config() is not None
    s['nzb_configured'] = len(get_newznab_scrapers()) > 0
    s['any_source_configured'] = s['zilean_configured'] or s['nzb_configured']
    results = get_last_results()
    if results and 'error' not in results:
        try:
            _nw      = _build_nw_filter()
            upgrades = [c for c in results.get('upgrade_candidates', []) if not _nw(c)]
            packs    = [c for c in results.get('pack_candidates', [])    if not _nw(c)]
        except Exception:
            upgrades = results.get('upgrade_candidates', [])
            packs    = results.get('pack_candidates', [])
        s['upgrade_count']    = len(upgrades)
        s['pack_count']       = len(packs)
        s['scanned_movies']   = results.get('scanned_movies', 0)
        s['scanned_episodes'] = results.get('scanned_episodes', 0)
        s['error_count']      = len(results.get('errors', []))
        s['used_db']          = results.get('used_db', False)
    return jsonify(s)


@upgrade_hub_bp.route('/api/scan', methods=['POST'])
@admin_required
def start_scan():
    from database.zilean_upgrade import scan_for_upgrades, get_scan_status, get_zilean_config
    from database.nzb_upgrade import get_newznab_scrapers

    if get_scan_status()['in_progress']:
        return jsonify({'success': False, 'error': 'Scan already in progress'}), 409

    upgrade_source = get_setting('Upgrade Hub', 'upgrade_source', 'both') or 'both'
    use_zilean = upgrade_source in ('both', 'zilean_only')
    use_nzb    = upgrade_source in ('both', 'nzb_only')

    if use_zilean and not use_nzb and not get_zilean_config():
        return jsonify({'success': False,
                        'error': 'Zilean scraper not enabled or URL not configured in Settings → Scrapers'}), 400
    if use_nzb and not use_zilean and not get_newznab_scrapers():
        return jsonify({'success': False,
                        'error': 'No enabled Newznab indexers configured in Settings → Scrapers'}), 400
    if not get_zilean_config() and not get_newznab_scrapers():
        return jsonify({'success': False,
                        'error': 'No upgrade sources configured. Enable Zilean or a Newznab indexer in Settings → Scrapers'}), 400

    scan_limit = get_setting('Upgrade Hub', 'scan_limit', None)
    if scan_limit is not None:
        try:
            scan_limit = int(scan_limit)
        except (TypeError, ValueError):
            scan_limit = None

    def _run():
        scan_for_upgrades(scan_limit=scan_limit)

    threading.Thread(target=_run, daemon=True, name='upgrade-hub-scan').start()
    try:
        from flask_login import current_user as _cu
        from utilities.ai_habits import track_action
        _uid = _cu.username if _cu.is_authenticated else 'system'
        track_action('upgrade_scan_manual', user_id=_uid)
    except Exception:
        pass
    return jsonify({'success': True, 'message': 'Scan started'})


@upgrade_hub_bp.route('/api/results')
@admin_required
def results():
    import copy
    from database.zilean_upgrade import get_last_results, get_scan_status
    data = get_last_results()
    s    = get_scan_status()
    # Filter not-wanted magnets from displayed results so that items which
    # failed and were registered to not_wanted disappear immediately without
    # requiring a full rescan. Load the set once to avoid N file reads.
    if data and 'error' not in data:
        try:
            data = copy.deepcopy(data)
            _nw  = _build_nw_filter()
            data['upgrade_candidates'] = [c for c in data.get('upgrade_candidates', []) if not _nw(c)]
            data['pack_candidates']    = [c for c in data.get('pack_candidates', [])    if not _nw(c)]
        except Exception:
            pass
    return jsonify({
        'success':     True,
        'in_progress': s['in_progress'],
        'results':     data,
    })


# ---------------------------------------------------------------------------
# Ignore selected candidates (add their magnets to not_wanted)
# ---------------------------------------------------------------------------

@upgrade_hub_bp.route('/api/ignore', methods=['POST'])
@admin_required
def ignore_candidates():
    from database.not_wanted_magnets import add_to_not_wanted, normalize_title as _nt
    from database.upgrade_hub_activity import add_ignored_items
    data    = request.get_json(force=True) or {}
    magnets = data.get('magnets', [])
    titles  = data.get('titles', [])
    items   = data.get('items', [])   # [{imdb_id, season, episode, item_type, title}, ...]
    if not magnets and not titles and not items:
        return jsonify({'success': False, 'error': 'No magnets, titles or items provided'}), 400
    added = 0
    for magnet in magnets:
        try:
            add_to_not_wanted(magnet)
            added += 1
        except Exception as e:
            logging.warning(f"[UPGRADE_HUB] Could not add magnet to not_wanted: {e}")
    titles_added = 0
    for title in titles:
        try:
            if title:
                add_to_not_wanted(_nt(title))
                titles_added += 1
        except Exception as e:
            logging.warning(f"[UPGRADE_HUB] Could not add title to not_wanted: {e}")
    # Persist item-level ignores (imdb_id + season) so different torrents for the
    # same show/season are also filtered in future scans, regardless of magnet/title.
    items_added = 0
    if items:
        items_added = add_ignored_items(items)
    logging.info(
        f"[UPGRADE_HUB] Ignored {added} magnet(s) + {titles_added} title(s) + "
        f"{items_added} item-level ignore(s)"
    )
    return jsonify({'success': True, 'added': max(added, titles_added, items_added)})


@upgrade_hub_bp.route('/api/ignored', methods=['GET'])
@admin_required
def list_ignored():
    from database.upgrade_hub_activity import get_ignored_items_list
    return jsonify({'success': True, 'ignored': get_ignored_items_list()})


@upgrade_hub_bp.route('/api/unignore', methods=['POST'])
@admin_required
def unignore_item():
    from database.upgrade_hub_activity import remove_ignored_item
    data = request.get_json(force=True) or {}
    row_id = data.get('id')
    if not row_id:
        return jsonify({'success': False, 'error': 'No id provided'}), 400
    ok = remove_ignored_item(int(row_id))
    return jsonify({'success': ok})


# ---------------------------------------------------------------------------
# Queue selected candidates
# ---------------------------------------------------------------------------

@upgrade_hub_bp.route('/api/queue', methods=['POST'])
@admin_required
def queue_items():
    from database.zilean_upgrade import queue_upgrade_candidates
    from flask import current_app
    data = request.get_json(force=True)
    item_ids = data.get('item_ids', [])
    if not item_ids:
        return jsonify({'success': False, 'error': 'No item_ids provided'}), 400
    result = queue_upgrade_candidates([int(i) for i in item_ids], triggered_by='manual')
    if result.get('queued', 0) > 0:
        try:
            runner = getattr(current_app, 'program_runner', None)
            if runner:
                # update() loads newly-queued items from DB into self.items before process() runs
                try:
                    runner.queue_manager.queues['Upgrading'].update()
                except Exception as e:
                    logging.warning(f"[UPGRADE_HUB] Could not update Upgrading queue: {e}")
                runner.trigger_task('Upgrading')
        except Exception as e:
            logging.warning(f"[UPGRADE_HUB] Could not trigger Upgrading task: {e}")
    return jsonify({'success': True, **result})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS_KEYS = {
    'min_improvement_threshold': int,
    'scan_limit': lambda v: int(v) if v not in (None, '', 'null') else None,
    'max_upgrades_per_run': int,
    'show_recent_only': bool,
    'hide_pack_episodes': bool,
    'recent_threshold_days': int,
    'excluded_genres': str,
    'exclude_nas_items': bool,
    'upgrade_source': str,
}
_SETTINGS_DEFAULTS = {
    'min_improvement_threshold': 0,
    'scan_limit': None,
    'max_upgrades_per_run': 10,
    'show_recent_only': False,
    'hide_pack_episodes': False,
    'recent_threshold_days': 90,
    'excluded_genres': '',
    'exclude_nas_items': False,
    'upgrade_source': 'both',
}


@upgrade_hub_bp.route('/api/settings', methods=['GET'])
@admin_required
def get_settings():
    settings = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        val = get_setting('Upgrade Hub', key, default)
        # Coerce stored strings back to correct types
        if key == 'scan_limit':
            settings[key] = int(val) if val not in (None, '', 'null', 0) else None
        elif key in ('min_improvement_threshold', 'max_upgrades_per_run'):
            try:
                settings[key] = int(val or default)
            except (TypeError, ValueError):
                settings[key] = default
        elif key in ('show_recent_only', 'hide_pack_episodes', 'exclude_nas_items'):
            settings[key] = bool(val) if not isinstance(val, str) else val.lower() == 'true'
        else:
            settings[key] = val
    return jsonify({'success': True, 'settings': settings})


@upgrade_hub_bp.route('/api/settings', methods=['POST'])
@admin_required
def save_settings():
    data = request.get_json(force=True) or {}
    for key in _SETTINGS_KEYS:
        if key not in data:
            continue
        value = data[key]
        # Normalise scan_limit: None/null → store as empty string so get_setting returns None
        if key == 'scan_limit':
            set_setting('Upgrade Hub', key, '' if value is None else int(value))
        elif key in ('show_recent_only', 'hide_pack_episodes', 'exclude_nas_items'):
            set_setting('Upgrade Hub', key, bool(value))
        else:
            try:
                set_setting('Upgrade Hub', key, int(value))
            except (TypeError, ValueError):
                set_setting('Upgrade Hub', key, value)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Genres list (for excluded-genres chip selector)
# ---------------------------------------------------------------------------

@upgrade_hub_bp.route('/api/genres')
@admin_required
def get_genres():
    import json as _json
    import re as _re
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT genres FROM media_items WHERE genres IS NOT NULL AND genres != ''"
        ).fetchall()
    finally:
        conn.close()

    # Normalise a single genre string: strip quotes/brackets, collapse hyphens to spaces,
    # title-case, skip empty or obviously junk tokens.
    def _clean(token: str) -> str:
        t = token.strip(" \t\r\n[]'\"")   # strip brackets and quote chars
        t = t.strip()
        t = _re.sub(r'-', ' ', t)          # "Science-Fiction" → "Science Fiction"
        t = _re.sub(r'\s*/\s*', ' & ', t)  # "Action/Adventure" → "Action & Adventure"
        t = t.title()
        return t if len(t) > 1 else ''

    raw_genres: set = set()
    for row in rows:
        g = row[0]
        # Try JSON first (handles ["Drama","Comedy"] stored as a real JSON string)
        try:
            parsed = _json.loads(g)
            if isinstance(parsed, list):
                for x in parsed:
                    c = _clean(str(x)) if x else ''
                    if c:
                        raw_genres.add(c)
                continue
        except (ValueError, TypeError):
            pass
        # Fallback: comma-separated (or single value), possibly with stray quotes/brackets
        for token in g.split(','):
            c = _clean(token)
            if c:
                raw_genres.add(c)

    # Deduplicate case-insensitively (keep title-cased form)
    seen_lower: dict = {}
    for g in sorted(raw_genres):
        key = g.lower()
        if key not in seen_lower:
            seen_lower[key] = g

    return jsonify({'success': True, 'genres': sorted(seen_lower.values())})


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@upgrade_hub_bp.route('/api/check_upgraded', methods=['POST'])
@admin_required
def check_upgraded():
    """Return which item IDs have current_score >= the candidate new_score (upgrade happened)."""
    data = request.get_json(silent=True) or {}
    # candidates: list of {id, min_score}
    candidates = data.get('candidates', [])
    if not candidates:
        return jsonify({'success': True, 'processed_ids': []})
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        processed_ids = []
        for c in candidates:
            try:
                item_id = int(c['id'])
                min_score = float(c['min_score'])
            except (KeyError, ValueError, TypeError):
                continue
            row = conn.execute(
                "SELECT current_score FROM media_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row and row['current_score'] is not None and row['current_score'] >= min_score:
                processed_ids.append(item_id)
    finally:
        conn.close()
    return jsonify({'success': True, 'processed_ids': processed_ids})


@upgrade_hub_bp.route('/api/clear_hub_queue', methods=['POST'])
@admin_required
def clear_hub_queue():
    """Revert hub-queued Upgrading items back to Collected and delete their pre-seeded magnets."""
    from database.core import get_db_connection
    from database.zilean_upgrade import _queued_magnets
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT item_id FROM upgrade_hub_queued_magnets"
        ).fetchall()
        item_ids = [r['item_id'] for r in rows]
        reverted = 0
        if item_ids:
            placeholders = ','.join('?' * len(item_ids))
            cur = conn.execute(
                f"UPDATE media_items SET state='Collected', upgrading=0, last_updated=datetime('now')"
                f" WHERE id IN ({placeholders}) AND state='Upgrading'",
                item_ids
            )
            reverted = cur.rowcount
            conn.execute("DELETE FROM upgrade_hub_queued_magnets")
            conn.commit()
            for iid in item_ids:
                _queued_magnets.pop(iid, None)
        conn.close()
        logging.info(f"[UPGRADE_HUB] Cleared hub queue: reverted {reverted} items to Collected")
        return jsonify({'success': True, 'reverted': reverted})
    except Exception as e:
        logging.error(f"[UPGRADE_HUB] clear_hub_queue failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@upgrade_hub_bp.route('/api/run_cleanup', methods=['POST'])
@admin_required
def run_cleanup():
    """Find all completed upgrades that still have the old torrent/file and clean them up."""
    from database.core import get_db_connection
    from database.collected_items import remove_original_item_from_plex, remove_original_item_from_account
    try:
        conn = get_db_connection()
        rows = conn.execute("""
            SELECT id, title, type, imdb_id, upgrading_from, filled_by_file,
                   upgrading_from_torrent_id, location_on_disk, season_number, episode_number,
                   version, resolution
            FROM media_items
            WHERE state = 'Collected'
              AND upgrading_from IS NOT NULL
              AND upgrading_from != ''
              AND filled_by_file IS NOT NULL
              AND upgrading_from != filled_by_file
              AND upgrading_from_torrent_id IS NOT NULL
        """).fetchall()
        conn.close()
    except Exception as e:
        logging.error(f"[UPGRADE_HUB] run_cleanup query failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

    if not rows:
        return jsonify({'success': True, 'cleaned': 0, 'message': 'Nothing to clean up'})

    def _do_cleanup():
        from database.core import get_db_connection as _get_db
        cleaned = 0
        conn2 = _get_db()
        try:
            for row in rows:
                item = dict(row)
                item['filled_by_torrent_id'] = item.pop('upgrading_from_torrent_id')
                try:
                    remove_original_item_from_plex(item)
                except Exception as e:
                    logging.warning(f"[UPGRADE_HUB] Plex cleanup failed for {item.get('title')}: {e}")
                try:
                    remove_original_item_from_account(item)
                    # Mark as processed so re-running cleanup won't pick it up again
                    conn2.execute(
                        "UPDATE media_items SET upgrading_from_torrent_id = NULL WHERE id = ?",
                        (item['id'],)
                    )
                    conn2.commit()
                    cleaned += 1
                except Exception as e:
                    logging.warning(f"[UPGRADE_HUB] Account cleanup failed for {item.get('title')}: {e}")
        finally:
            conn2.close()
        logging.info(f"[UPGRADE_HUB] Manual cleanup complete: {cleaned}/{len(rows)} items processed")

    threading.Thread(target=_do_cleanup, daemon=True, name='upgrade-hub-cleanup').start()
    return jsonify({'success': True, 'queued': len(rows),
                    'message': f'Cleanup started for {len(rows)} item(s) in background'})


@upgrade_hub_bp.route('/api/activity')
@admin_required
def get_activity():
    from database.upgrade_hub_activity import get_hub_activity
    limit  = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    atype  = request.args.get('type') or None
    rows, total = get_hub_activity(limit=limit, offset=offset, action_type=atype)
    return jsonify({'success': True, 'activities': rows, 'total': total})
