"""Activity, preview posters/clearlogos, logos and fonts."""

import re
from flask import jsonify, request, send_file, abort
from pathlib import Path
from utilities.paths import config_dir
from database.core import get_db_connection as _get_db_connection
from ._common import _SYSTEM_LOGO_DIR, _user_logo_dir, logger, overlay_bp

@overlay_bp.route('/api/overlays/activity', methods=['GET'])
def get_overlay_activity():
    """Return recent overlay activity log rows (newest first).

    Query params:
        limit  — max rows to return (default 50, max 200)
        offset — pagination offset (default 0)
        type   — filter by action_type (optional)
    """
    try:
        limit  = min(int(request.args.get('limit', 50)), 200)
        offset = max(int(request.args.get('offset', 0)), 0)
        action_filter = request.args.get('type', '').strip()

        conn = _get_db_connection()
        cursor = conn.cursor()

        if action_filter:
            cursor.execute(
                '''SELECT id, action_type, triggered_by, result, title, stats_json, duration_seconds, created_at
                   FROM overlay_activity
                   WHERE action_type = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?''',
                (action_filter, limit, offset)
            )
        else:
            cursor.execute(
                '''SELECT id, action_type, triggered_by, result, title, stats_json, duration_seconds, created_at
                   FROM overlay_activity
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?''',
                (limit, offset)
            )
        rows = cursor.fetchall()

        cursor.execute('SELECT COUNT(*) FROM overlay_activity' +
                       (' WHERE action_type = ?' if action_filter else ''),
                       (action_filter,) if action_filter else ())
        total = cursor.fetchone()[0]
        conn.close()

        import json as _json
        activities = []
        for row in rows:
            stats = None
            if row['stats_json']:
                try:
                    stats = _json.loads(row['stats_json'])
                except Exception:
                    stats = None
            activities.append({
                'id':               row['id'],
                'action_type':      row['action_type'],
                'triggered_by':     row['triggered_by'],
                'result':           row['result'],
                'title':            row['title'],
                'stats':            stats,
                'duration_seconds': row['duration_seconds'],
                'created_at':       row['created_at'],
            })

        return jsonify({'success': True, 'activities': activities, 'total': total})
    except Exception as e:
        logger.error(f"Failed to get overlay activity: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/preview/posters', methods=['GET'])
def get_preview_posters():
    """Return random poster URLs from poster cache for layout builder preview cycling.

    Cache keys are formatted as "{tmdb_id}_{type}" (e.g. "12345_movie", "67890_tv").
    Values are (url, cached_at) tuples where url is a full TMDB CDN URL.
    Meta/trending entries are skipped automatically.

    When textless=1 is passed, attempts to return the null-language (textless) TMDB
    poster for each candidate instead of the cached English one.
    Also returns tmdb_id and media_type per entry so the builder can fetch clearlogos.
    """
    import random as _random
    from datetime import datetime, timedelta
    from routes.poster_cache import load_cache

    media_type = request.args.get('type', 'both')
    limit = min(int(request.args.get('limit', 20)), 50)
    textless = request.args.get('textless', '0') == '1'

    type_filters = set()
    if media_type in ('movie', 'both'):
        type_filters.add('movie')
    if media_type in ('tv', 'both'):
        type_filters.add('tv')

    cache = load_cache()
    expiry = timedelta(days=7)
    now = datetime.now()
    candidates = []

    for cache_key, value in cache.items():
        try:
            url, cached_at = value
        except (TypeError, ValueError):
            continue

        # Skip non-URL entries (meta dicts, trending response, etc.)
        if not isinstance(url, str) or not url.startswith('http'):
            continue
        if now - cached_at > expiry:
            continue

        # Match type suffix: key ends with "_movie" or "_tv"
        for t in type_filters:
            if cache_key.endswith(f'_{t}'):
                # Extract tmdb_id from key (format: "{tmdb_id}_{type}")
                tmdb_id = cache_key[: -(len(t) + 1)]
                candidates.append({
                    'poster_url': url,
                    'type': t,
                    'tmdb_id': tmdb_id,
                })
                break

    _random.shuffle(candidates)
    selected = candidates[:limit]

    # For textless mode, swap poster URLs to null-language TMDB posters
    if textless:
        from utilities.settings import get_setting as _gs
        _tmdb_key = _gs('TMDB', 'api_key', default='')
        import requests as _req
        for entry in selected:
            try:
                _mtype = 'tv' if entry['type'] == 'tv' else 'movie'
                _r = _req.get(
                    f"https://api.themoviedb.org/3/{_mtype}/{entry['tmdb_id']}/images"
                    f"?api_key={_tmdb_key}&include_image_language=null",
                    timeout=8
                )
                if _r.status_code == 200:
                    _posters = _r.json().get('posters', [])
                    if _posters:
                        # Prefer voted posters — filters out obscure foreign uploads (vote_count=0)
                        _voted = [p for p in _posters if p.get('vote_count', 0) >= 3]
                        _cands = _voted if _voted else _posters
                        _cands.sort(
                            key=lambda p: p.get('vote_count', 0) * p.get('vote_average', 0),
                            reverse=True)
                        entry['poster_url'] = (
                            f"https://image.tmdb.org/t/p/w300{_cands[0]['file_path']}")
            except Exception:
                pass  # keep original cached URL on error

    return jsonify({'posters': selected})

@overlay_bp.route('/api/overlays/preview/clearlogo', methods=['GET'])
def get_preview_clearlogo():
    """Return the best English clearlogo URL for a given TMDB ID.

    Query params: tmdb_id, type (movie|tv)
    Returns: { logo_url: str|null }
    """
    tmdb_id   = request.args.get('tmdb_id', '').strip()
    item_type = request.args.get('type', 'movie')
    if not tmdb_id:
        return jsonify({'logo_url': None})
    try:
        from utilities.settings import get_setting as _gs
        import requests as _req
        _api_key = _gs('TMDB', 'api_key', default='')
        if not _api_key:
            return jsonify({'logo_url': None})
        _mtype = 'tv' if item_type == 'tv' else 'movie'
        logo_url = None
        for _lang in ('en', 'en,null'):
            _r = _req.get(
                f"https://api.themoviedb.org/3/{_mtype}/{tmdb_id}/images"
                f"?api_key={_api_key}&include_image_language={_lang}",
                timeout=10
            )
            if _r.status_code == 200:
                _logos = _r.json().get('logos', [])
                _png = [l for l in _logos if (l.get('file_path') or '').endswith('.png')]
                _best = sorted(_png or _logos, key=lambda l: l.get('vote_average', 0), reverse=True)
                if _best:
                    logo_url = f"https://image.tmdb.org/t/p/original{_best[0]['file_path']}"
                    break
        return jsonify({'logo_url': logo_url})
    except Exception as e:
        logger.warning(f"Preview clearlogo fetch failed for {tmdb_id}: {e}")
        return jsonify({'logo_url': None})

# ── Logo Library ───────────────────────────────────────────────────────────────

def _scan_logo_dir(base: Path, is_user: bool) -> dict:
    """Scan a logo directory and return groups keyed by 'category' or 'category/subcat'."""
    groups = {}
    if not base.exists():
        return groups
    for cat_dir in sorted(base.iterdir()):
        if not cat_dir.is_dir():
            continue
        cat_name = cat_dir.name
        # Flat PNGs directly inside category dir
        flat_pngs = sorted(cat_dir.glob('*.png'), key=lambda p: p.stem.lower())
        if flat_pngs:
            key = cat_name
            if key not in groups:
                groups[key] = {'category': cat_name, 'subcat': None, 'logos': []}
            for p in flat_pngs:
                rel = f"{cat_name}/{p.name}"
                groups[key]['logos'].append({
                    'name': p.stem, 'filename': p.name,
                    'path': f"/logos/{rel}",
                    'url': f"/api/overlays/logos/serve/{rel}",
                    'is_user': is_user,
                })
        # Sub-category dirs
        for sub_dir in sorted(cat_dir.iterdir()):
            if not sub_dir.is_dir():
                continue
            sub_pngs = sorted(sub_dir.glob('*.png'), key=lambda p: p.stem.lower())
            if sub_pngs:
                key = f"{cat_name}/{sub_dir.name}"
                if key not in groups:
                    groups[key] = {'category': cat_name, 'subcat': sub_dir.name, 'logos': []}
                for p in sub_pngs:
                    rel = f"{cat_name}/{sub_dir.name}/{p.name}"
                    groups[key]['logos'].append({
                        'name': p.stem, 'filename': p.name,
                        'path': f"/logos/{rel}",
                        'url': f"/api/overlays/logos/serve/{rel}",
                        'is_user': is_user,
                    })
    return groups

@overlay_bp.route('/api/overlays/logos', methods=['GET'])
def list_logos():
    """List available logos grouped by category/subcategory.
    Optional ?category=rating|network|studio to filter."""
    category_filter = request.args.get('category')

    sys_groups  = _scan_logo_dir(_SYSTEM_LOGO_DIR, False)
    user_groups = _scan_logo_dir(_user_logo_dir(),   True)

    # Merge: user logos added on top of system; same key → merge logo lists
    merged = dict(sys_groups)
    for key, ug in user_groups.items():
        if key not in merged:
            merged[key] = ug
        else:
            user_paths = {l['path'] for l in ug['logos']}
            combined = ug['logos'] + [l for l in merged[key]['logos'] if l['path'] not in user_paths]
            combined.sort(key=lambda l: l['name'].lower())
            merged[key] = dict(merged[key], logos=combined)

    # Build category tree
    categories: dict = {}
    for key, grp in merged.items():
        cat = grp['category']
        if category_filter and cat != category_filter:
            continue
        if cat not in categories:
            categories[cat] = {'name': cat, 'groups': []}
        categories[cat]['groups'].append({
            'key': key,
            'label': grp['subcat'] if grp['subcat'] else cat,
            'logos': grp['logos'],
        })

    return jsonify({
        'categories': [{'name': n, 'groups': v['groups']}
                       for n, v in sorted(categories.items())],
    })

@overlay_bp.route('/api/overlays/logos/serve/<path:filepath>', methods=['GET'])
def serve_logo(filepath):
    """Serve a logo file — user dir first, system fallback."""
    # Strip leading 'logos/' prefix if present (saved paths sometimes include it)
    if filepath.startswith('logos/'):
        filepath = filepath[len('logos/'):]
    parts = Path(filepath).parts
    if '..' in parts or Path(filepath).is_absolute() or Path(filepath).drive:
        abort(400)
    for base in (_user_logo_dir(), _SYSTEM_LOGO_DIR):
        path = (base / filepath).resolve()
        if path.is_relative_to(Path(base).resolve()) and path.is_file():
            return send_file(str(path))
    abort(404)

@overlay_bp.route('/api/overlays/logos/upload', methods=['POST'])
def upload_logo():
    """Upload a logo to the user logo directory."""
    f = request.files.get('file')
    category   = request.form.get('category', 'custom')
    subcategory = request.form.get('subcategory', '').strip()

    if not f or not f.filename:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    allowed = {'.png', '.jpg', '.jpeg', '.webp'}
    suffix = Path(f.filename).suffix.lower()
    if suffix not in allowed:
        return jsonify({'success': False, 'error': 'Only PNG/JPG/WebP accepted'}), 400

    safe_cat  = re.sub(r'[^\w\-]', '_', category)
    safe_sub  = re.sub(r'[^\w\-]', '_', subcategory) if subcategory else ''
    safe_name = re.sub(r'[^\w\-. ]', '_', Path(f.filename).name)

    dest_dir = _user_logo_dir() / safe_cat / safe_sub if safe_sub else _user_logo_dir() / safe_cat
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    f.save(str(dest))

    rel = str(dest.relative_to(_user_logo_dir()))
    return jsonify({
        'success': True,
        'path': f"/logos/{rel}",
        'url': f"/api/overlays/logos/serve/{rel}",
    })

@overlay_bp.route('/api/overlays/logos/delete', methods=['POST'])
def delete_logo():
    """Delete a user-uploaded logo (cannot delete system logos)."""
    raw = (request.json or {}).get('path', '')
    if not raw:
        return jsonify({'success': False, 'error': 'No path provided'}), 400

    # Strip /logos/ prefix to get relative path
    rel = raw.lstrip('/')
    if rel.startswith('logos/'):
        rel = rel[len('logos/'):]

    parts = Path(rel).parts
    if '..' in parts or not parts:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400

    user_path = _user_logo_dir() / rel
    try:
        resolved = user_path.resolve()
        base_resolved = _user_logo_dir().resolve()
        if not str(resolved).startswith(str(base_resolved)):
            return jsonify({'success': False, 'error': 'Path outside user logo directory'}), 400
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400

    if user_path.exists():
        user_path.unlink()
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'File not found or not a user logo'}), 404

@overlay_bp.route('/api/overlays/fonts/<font_name>', methods=['GET'])
def serve_font(font_name):
    """Serve a font file for use in the layout builder canvas.

    Allowed font names must be alphanumeric + dash/underscore with .ttf or .otf extension.
    Searches user asset dir first, then system fonts.
    """
    import re as _re
    if not _re.match(r'^[\w\-]+\.(ttf|otf)$', font_name, _re.IGNORECASE):
        abort(400)

    stem = Path(font_name).stem

    search_paths = [
        Path(config_dir('overlay_assets', 'fonts')) / font_name,
        Path(__file__).parent.parent.parent / 'overlays' / 'fonts' / 'cache' / font_name,
        Path('/usr/share/fonts/TTF') / font_name,
        Path('/usr/share/fonts/truetype/dejavu') / font_name,
        Path(f'/usr/share/fonts/truetype/{stem.lower()}') / font_name,
    ]
    for p in search_paths:
        if p.exists():
            mime = 'font/otf' if font_name.lower().endswith('.otf') else 'font/ttf'
            return send_file(str(p), mimetype=mime)
    abort(404)
