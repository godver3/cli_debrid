from flask import Blueprint, request, Response, jsonify, stream_with_context
from flask_login import current_user
import requests
import json
import logging

from utilities.settings import get_setting, load_config, save_config
from utilities.ai_context import build_system_prompt
from routes.models import admin_required

ai_bp = Blueprint('ai', __name__)

logger = logging.getLogger(__name__)


def _get_openclaw_config():
    enabled = get_setting('AI Assistant', 'enabled', False)
    url = get_setting('AI Assistant', 'openclaw_url', '').rstrip('/')
    token = get_setting('AI Assistant', 'openclaw_token', '')
    agent_id = get_setting('AI Assistant', 'agent_id', 'main')
    return enabled, url, token, agent_id


def _get_display_name():
    """Return the configured AI Butler display name, falling back to 'AI Butler'."""
    return get_setting('AI Assistant', 'display_name', '') or 'AI Butler'


def _to_http_url(url):
    """Ensure URL uses http(s):// — convert wss:// or ws:// if user entered those."""
    if url.startswith('wss://'):
        return 'https://' + url[6:]
    if url.startswith('ws://'):
        return 'http://' + url[5:]
    return url


import re as _re

def _get_exclusion_titles() -> list:
    """
    Return a compact list of "Title (Year)" strings for all collected movies/shows
    and watch history entries. Used to tell the AI what NOT to recommend.
    Capped at 300 entries to stay token-efficient.
    """
    titles = set()
    try:
        from database import get_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        # Collected movies (distinct title+year)
        c.execute("""
            SELECT DISTINCT title, year FROM media_items
            WHERE type='movie' AND state IN ('Collected','Upgrading','Wanted')
            ORDER BY title
        """)
        for title, year in c.fetchall():
            titles.add(f"{title} ({year})" if year else title)
        # Collected shows
        c.execute("""
            SELECT DISTINCT title, year FROM media_items
            WHERE type='episode' AND state IN ('Collected','Upgrading','Wanted')
            GROUP BY title, year
            ORDER BY title
        """)
        for title, year in c.fetchall():
            titles.add(f"{title} ({year})" if year else title)
        conn.close()
    except Exception as e:
        logger.debug(f"AI exclusion titles: DB failed: {e}")

    # Also pull watch history if available
    try:
        import os, sqlite3
        watch_db = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'watch_history.db')
        if os.path.exists(watch_db):
            wconn = sqlite3.connect(watch_db)
            wc = wconn.cursor()
            wc.execute("SELECT DISTINCT title, year FROM watch_history ORDER BY title")
            for title, year in wc.fetchall():
                titles.add(f"{title} ({year})" if year else title)
            wconn.close()
    except Exception as e:
        logger.debug(f"AI exclusion titles: watch_history failed: {e}")

    return sorted(titles)


def _mdblist_search(title: str, year, media_type: str):
    """
    Search MDBList by title+year and return dict with imdb_id, tmdb_id, media_type — or None.
    MDBList indexes English aliases so it finds non-English films by English title.
    Requires MDBList API key to be configured.
    """
    try:
        from utilities.mdblist_api import get_mdblist_api_key, MDBLIST_API_BASE
        import requests as _req
        api_key = get_mdblist_api_key()
        if not api_key or not title:
            return None
        def _norm_title(s):
            return s.lower().replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"')

        def _fetch_mdb(query, yr=None):
            params = f"apikey={api_key}&s={_req.utils.quote(query)}&limit=50"
            if yr:
                params += f"&y={yr}"
            url = f"{MDBLIST_API_BASE}/?{params}"
            r = _req.get(url, timeout=10)
            if not r.ok:
                return []
            data = r.json()
            return data if isinstance(data, list) else data.get('results', data.get('search', []))

        title_norm = _norm_title(title)
        year_int = int(year) if year else None
        # Try with year first (most specific), then without year as fallback
        items = _fetch_mdb(title, year_int)
        if not items:
            items = _fetch_mdb(title)
        # Also try stripped title (removes apostrophes/special chars) as fallback
        stripped_title = title.replace("'", "").replace("\u2019", "").replace("\u2018", "")
        if stripped_title != title:
            stripped_items = _fetch_mdb(stripped_title, year_int)
            if not stripped_items:
                stripped_items = _fetch_mdb(stripped_title)
            items = items + stripped_items
        logger.info(f"AI add_to_library: MDBList search '{title}' ({year}) → {len(items)} results: {[i.get('title') for i in items[:5]]}")
        for item in items:
            t = _norm_title(item.get('title') or '')
            y = item.get('year')
            mt = 'tv' if item.get('mediatype', 'movie') in ('show', 'tv', 'series') else 'movie'
            if t == title_norm and (not year_int or y == year_int):
                imdb = item.get('imdb_id') or item.get('imdbid') or item.get('imdb')
                tmdb_raw = item.get('tmdb_id')
                # Never use item.get('id') — on the search endpoint that field contains the IMDb ID
                tmdb = str(tmdb_raw) if tmdb_raw else ''
                # Use TMDB /find to fill in whichever ID is missing
                if imdb and not tmdb:
                    try:
                        import requests as _req2
                        from utilities.settings import get_setting as _gs2
                        tmdb_key = _gs2('TMDB', 'api_key', '')
                        if tmdb_key:
                            fr = _req2.get(
                                'https://api.themoviedb.org/3/find/' + imdb,
                                params={'api_key': tmdb_key, 'external_source': 'imdb_id'}, timeout=5
                            )
                            if fr.ok:
                                res = fr.json()
                                found = res.get('movie_results') or res.get('tv_results') or []
                                if found:
                                    tmdb = str(found[0].get('id', ''))
                    except Exception:
                        pass
                elif tmdb and not imdb:
                    try:
                        import requests as _req2
                        from utilities.settings import get_setting as _gs2
                        tmdb_key = _gs2('TMDB', 'api_key', '')
                        if tmdb_key:
                            fr = _req2.get(
                                f'https://api.themoviedb.org/3/{"tv" if mt == "tv" else "movie"}/{tmdb}/external_ids',
                                params={'api_key': tmdb_key}, timeout=5
                            )
                            if fr.ok:
                                imdb = fr.json().get('imdb_id') or imdb
                    except Exception:
                        pass
                if imdb or tmdb:
                    logger.info(f"MDBList: '{title}' ({year}) → imdb={imdb} tmdb={tmdb}")
                    return {'imdb_id': imdb, 'tmdb_id': tmdb or None, 'media_type': mt}
    except Exception as e:
        logger.debug(f"MDBList search failed for '{title}': {e}")
    return None


def _trakt_lookup_by_imdb(imdb_id: str):
    """
    Call Trakt /search/imdb/{id} and return a dict with confirmed title, year,
    imdb_id, tmdb_id, media_type — or None if not found.
    """
    try:
        trakt_client_id = get_setting('Trakt', 'client_id')
        if not trakt_client_id or not imdb_id:
            return None
        import requests as _req
        headers = {'Content-Type': 'application/json', 'trakt-api-version': '2', 'trakt-api-key': trakt_client_id}
        r = _req.get(f"https://api.trakt.tv/search/imdb/{imdb_id}", headers=headers, timeout=10)
        if not r.ok or not r.json():
            logger.info(f"Trakt /search/imdb/{imdb_id} → {r.status_code} empty")
            return None
        item = r.json()[0]
        mtype = item.get('type')  # 'movie' or 'show'
        details = item.get(mtype, {})
        ids = details.get('ids', {})
        result = {
            'title': details.get('title'),
            'year': details.get('year'),
            'imdb_id': ids.get('imdb'),
            'tmdb_id': str(ids['tmdb']) if ids.get('tmdb') else None,
            'media_type': 'tv' if mtype == 'show' else 'movie',
        }
        logger.info(f"Trakt /search/imdb/{imdb_id} → {result}")
        return result
    except Exception as e:
        logger.debug(f"Trakt imdb lookup failed for {imdb_id}: {e}")
        return None


def _enrich_library_blocks(content: str) -> str:
    """
    Server-side post-processing of AI responses containing ADD_TO_LIBRARY blocks.

    For each ADD_TO_LIBRARY block:
    - Look up the title in the DB (by imdb_id or title+year)
    - If already collected: REMOVE the block and insert an inline note instead
    - If not collected: inject tmdb_id into the block (for linking) if found in DB as Wanted
    - If not in DB at all: look up tmdb_id via Trakt/TMDB and inject it

    This runs server-side so the AI never needs to cooperate with SEARCH_LIBRARY.
    """
    pattern = _re.compile(
        r'^ADD_TO_LIBRARY:\s*(\{[^\n]+\})\s*$',
        _re.MULTILINE
    )

    def _lookup_db(title, year, imdb_id, media_type):
        """Returns (state, tmdb_id) or (None, None) if not found."""
        try:
            from database import get_db_connection
            conn = get_db_connection()
            c = conn.cursor()
            # Try imdb_id first
            if imdb_id:
                c.execute(
                    "SELECT state, tmdb_id FROM media_items WHERE imdb_id=? "
                    "AND state IN ('Collected','Upgrading','Wanted') LIMIT 1",
                    (imdb_id,)
                )
                row = c.fetchone()
                if row:
                    conn.close()
                    return row[0], row[1]
            # Fall back to exact title + year (no fuzzy match to avoid wrong-movie false positives)
            if title:
                if year:
                    c.execute(
                        "SELECT state, tmdb_id FROM media_items WHERE title=? AND year=? "
                        "AND state IN ('Collected','Upgrading','Wanted') LIMIT 1",
                        (title, str(year))
                    )
                else:
                    c.execute(
                        "SELECT state, tmdb_id FROM media_items WHERE title=? "
                        "AND state IN ('Collected','Upgrading','Wanted') LIMIT 1",
                        (title,)
                    )
                row = c.fetchone()
                if row:
                    conn.close()
                    return row[0], row[1]
            conn.close()
        except Exception as e:
            logger.debug(f"AI enrich: DB lookup failed: {e}")
        return None, None

    def _resolve_tmdb(title, year, media_type, imdb_id):
        """Resolve tmdb_id: Trakt ID lookup by imdb_id first, fall back to title+year search."""
        try:
            from utilities.web_scraper import search_trakt
            # Primary: imdb_id → Trakt /search/imdb → get confirmed tmdb_id directly
            if imdb_id:
                info = _trakt_lookup_by_imdb(imdb_id)
                if info and info.get('tmdb_id'):
                    logger.info(f"AI enrich: imdb {imdb_id} → Trakt confirmed '{info['title']}' tmdb={info['tmdb_id']}")
                    return info['tmdb_id']
                logger.info(f"AI enrich: Trakt imdb lookup for {imdb_id} failed — trying title search")
            # Fallback 1: MDBList exact title+year match
            if title:
                mdb = _mdblist_search(title, year, media_type)
                if mdb and mdb.get('tmdb_id'):
                    return mdb['tmdb_id']
            # Fallback 2: Trakt title+year text search — exact title match only
            # search_trakt() returns converted dicts: {title, year, media_type, id (tmdb_id), ...}
            if title:
                results = search_trakt(title, year)
                title_lower = title.lower()
                year_int = int(year) if year else None
                for r in results:
                    if (r.get('title') or '').lower() == title_lower and (not year_int or r.get('year') == year_int):
                        return str(r.get('id', ''))
                for r in results:
                    if (r.get('title') or '').lower() == title_lower:
                        return str(r.get('id', ''))
            # Fallback 3: TMDB /find/{imdb_id} — works when Trakt is rate-limited
            if imdb_id:
                try:
                    import requests as _req
                    from utilities.settings import get_setting as _gs
                    tmdb_key = _gs('TMDB', 'api_key', '')
                    if tmdb_key:
                        r = _req.get(
                            f'https://api.themoviedb.org/3/find/{imdb_id}',
                            params={'api_key': tmdb_key, 'external_source': 'imdb_id'},
                            timeout=5
                        )
                        if r.ok:
                            data = r.json()
                            results_list = data.get('movie_results') or data.get('tv_results') or []
                            if results_list:
                                tmdb_id = results_list[0].get('id')
                                if tmdb_id:
                                    logger.info(f"AI enrich: TMDB /find/{imdb_id} → tmdb={tmdb_id}")
                                    return str(tmdb_id)
                except Exception as e2:
                    logger.debug(f"AI enrich: TMDB find fallback failed: {e2}")
        except Exception as e:
            logger.debug(f"AI enrich: tmdb resolve failed: {e}")
        return None

    def _replace(m):
        raw_json = m.group(1)
        try:
            obj = json.loads(raw_json)
        except Exception:
            return m.group(0)  # malformed — leave as-is

        title = (obj.get('title') or '').strip()
        year = obj.get('year')
        imdb_id = (obj.get('imdb_id') or '').strip()
        media_type = (obj.get('media_type') or 'movie').lower()
        if media_type == 'show':
            media_type = 'tv'

        state, tmdb_id_db = _lookup_db(title, year, imdb_id, media_type)

        if state in ('Collected', 'Upgrading'):
            # Already collected — strip the block, leave a note
            type_label = 'show' if media_type == 'tv' else 'movie'
            link_part = ''
            if tmdb_id_db:
                path = f'/library/show/{tmdb_id_db}' if media_type == 'tv' else f'/library/movie/{tmdb_id_db}'
                link_part = f' [View in library]({path})'
            year_str = f' ({year})' if year else ''
            return f'*{title}{year_str} is already in your library.*{link_part}'

        # Not collected — always re-resolve tmdb_id server-side (AI frequently hallucinates IDs).
        # Priority: DB lookup > authoritative resolution (Trakt/MDBList/TMDB)
        # NEVER fall back to AI-provided tmdb_id — it is frequently wrong and causes wrong links.
        tmdb_id_final = tmdb_id_db or _resolve_tmdb(title, year, media_type, imdb_id)

        if tmdb_id_final:
            obj['tmdb_id'] = str(tmdb_id_final)
        else:
            # Strip any AI-provided tmdb_id rather than show a wrong discover link
            obj.pop('tmdb_id', None)

        return f'ADD_TO_LIBRARY: {json.dumps(obj, ensure_ascii=False)}'

    return pattern.sub(_replace, content)


@ai_bp.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """Proxy a chat message to OpenClaw."""
    from flask_login import current_user as cu
    from routes.utils import is_user_system_enabled

    if is_user_system_enabled() and not cu.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401

    enabled, openclaw_url, token, agent_id = _get_openclaw_config()

    if not enabled:
        return jsonify({'error': 'AI Assistant is not enabled. Configure it in Settings → AI Assistant.'}), 503

    if not openclaw_url:
        return jsonify({'error': 'OpenClaw URL is not configured. Go to Settings → AI Assistant.'}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    messages = data.get('messages', [])
    if not messages:
        return jsonify({'error': 'No messages provided'}), 400

    page = data.get('page', '')
    page_data = data.get('page_data', {})
    session_id = data.get('session_id', '')

    try:
        system_prompt = build_system_prompt(page=page, page_data=page_data)
    except Exception as e:
        logger.error(f"AI Butler: failed to build system prompt: {e}")
        system_prompt = f"You are the cli_debrid {_get_display_name()}. Help users configure and use the application."

    if is_user_system_enabled() and cu.is_authenticated:
        base_id = f"clidebrid_{cu.id}"
    else:
        base_id = "clidebrid_default"

    # Append session_id so each new session gets isolated OpenClaw memory
    user_id = f"{base_id}_{session_id}" if session_id else base_id

    # Detect if the last user message is a recommendation request.
    # If so, inject a format-enforcement assistant turn immediately before it.
    # This is more reliable than system prompt instructions because the model
    # treats its own prior "words" as higher-confidence context.
    augmented_messages = list(messages)
    _RECOMMEND_KEYWORDS = (
        'recommend', 'suggest', 'what should i watch', 'what to watch',
        'similar to', 'like my taste', 'based on my', 'movies i would like',
        'shows i would like', 'what would i like', 'what movies', 'what shows',
        'add movie', 'add show', 'add series', 'add to my library', 'add to library',
        'add to the library', 'add to queue', 'download movie', 'download show',
        'get movie', 'get show', 'queue movie', 'queue show',
    )
    last_user_msg = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'), '')
    _is_recommend = any(kw in last_user_msg.lower() for kw in _RECOMMEND_KEYWORDS)

    if _is_recommend and get_setting('AI Assistant', 'enable_recommendations', True):
        # Build a compact exclusion list of collected + watched titles so the AI
        # can avoid them at generation time, not just have them stripped after.
        _exclusion_titles = _get_exclusion_titles()
        _excl_block = ''
        if _exclusion_titles:
            _excl_block = (
                "\n\nDO NOT recommend any of these titles — they are already collected or watched:\n"
                + ', '.join(_exclusion_titles[:300])  # cap at 300 to stay compact
                + "\nRecommend ONLY titles not in this list."
            )

        _format_reminder = (
            "Understood. I will respond using ADD_TO_LIBRARY blocks for every title I recommend. "
            "Format (one per line, no code fences):\n"
            'ADD_TO_LIBRARY: {"title": "...", "year": 2023, "media_type": "movie", "reason": "..."}\n'
            "IMPORTANT: I will NEVER include imdb_id or tmdb_id unless I am 100% certain of the exact value — LLMs hallucinate IDs and wrong IDs cause failures. "
            "The server resolves correct IDs from title+year automatically. "
            "I will not use plain numbered lists. Each recommendation gets its own ADD_TO_LIBRARY line with a brief explanation above it."
            + _excl_block
        )
        # Insert as an assistant message just before the last user message
        if augmented_messages and augmented_messages[-1].get('role') == 'user':
            last = augmented_messages.pop()
            augmented_messages.append({'role': 'assistant', 'content': _format_reminder})
            augmented_messages.append(last)

    payload = {
        "model": f"openclaw:{agent_id}",
        "messages": [
            {"role": "system", "content": system_prompt},
            *augmented_messages
        ],
        "stream": False,
        "user": user_id
    }

    http_url = _to_http_url(openclaw_url)
    endpoint = f"{http_url}/v1/chat/completions"

    # Determine the public-facing URL of this cli_debrid instance so OpenClaw
    # can call back to tool endpoints without hitting a loopback/internal address.
    # Reuse SSO redirect_uri_base — it's already set to the public URL.
    _sso_base = get_setting('SSO', 'redirect_uri_base', '').strip().rstrip('/')
    if not _sso_base:
        _fwd_host = request.headers.get('X-Forwarded-Host') or request.host
        _fwd_proto = request.headers.get('X-Forwarded-Proto') or ('https' if request.is_secure else 'http')
        _sso_base = f"{_fwd_proto}://{_fwd_host}"

    headers = {
        "Content-Type": "application/json",
        "x-openclaw-agent-id": agent_id,
        "x-openclaw-skill-base-url": _sso_base,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    logger.info(f"AI Butler: POST {endpoint}")

    def generate():
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                error_body = resp.text[:500]
                logger.error(f"AI Butler: OpenClaw returned {resp.status_code}: {error_body}")
                yield f"data: {json.dumps({'error': f'OpenClaw error {resp.status_code}: {error_body}'})}\n\n"
                return

            resp_data = resp.json()
            content = resp_data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if content:
                content = _enrich_library_blocks(content)
                chunk = {"choices": [{"delta": {"content": content}}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        except requests.exceptions.ConnectionError as e:
            logger.error(f"AI Butler: connection error: {e}")
            yield f"data: {json.dumps({'error': f'Cannot connect to OpenClaw: {str(e)}'})}\n\n"
        except requests.exceptions.Timeout:
            yield f"data: {json.dumps({'error': 'OpenClaw request timed out.'})}\n\n"
        except Exception as e:
            logger.error(f"AI Butler: unexpected error: {type(e).__name__}: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': f'Unexpected error: {type(e).__name__}: {str(e)}'})}\n\n"

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers.update({
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    })
    return response


@ai_bp.route('/api/ai/apply_setting', methods=['POST'])
def ai_apply_setting():
    """Apply a single setting change suggested by the AI Butler.

    Body: { "section": "Scraping", "key": "enable_upgrading", "value": true }
    Returns: { "ok": true, "requires_restart": false }
    """
    from flask_login import current_user as cu
    from routes.utils import is_user_system_enabled
    from utilities.settings_schema import SETTINGS_SCHEMA

    if is_user_system_enabled() and not cu.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401

    if not get_setting('AI Assistant', 'enabled', False):
        return jsonify({'error': 'AI Assistant is disabled'}), 403

    if not get_setting('AI Assistant', 'enable_settings_assistant', True):
        return jsonify({'error': 'Settings Assistant (Phase 2) is disabled in AI Assistant settings'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    section = data.get('section', '').strip()
    key = data.get('key', '').strip()
    value = data.get('value')

    if not section or not key or value is None:
        return jsonify({'error': 'section, key and value are required'}), 400

    # Security allowlist — AI can only write these sections
    ALLOWED_SECTIONS = {
        'Scraping', 'File Management', 'Plex', 'Trakt',
        'UI Settings', 'Notifications', 'Debug', 'Discover Settings',
    }
    if section not in ALLOWED_SECTIONS:
        return jsonify({'error': f'Section "{section}" cannot be modified via AI Butler'}), 403

    # Validate key exists in schema
    section_schema = SETTINGS_SCHEMA.get(section, {})
    if key not in section_schema:
        return jsonify({'error': f'Unknown setting: {section}.{key}'}), 400

    key_schema = section_schema[key]
    expected_type = key_schema.get('type')

    # Type coercion + validation
    try:
        if expected_type == 'boolean':
            if isinstance(value, str):
                value = value.lower() in ('true', '1', 'yes')
            else:
                value = bool(value)
        elif expected_type == 'integer':
            value = int(value)
        elif expected_type == 'number':
            value = float(value)
        elif expected_type == 'string':
            value = str(value)
            choices = key_schema.get('choices')
            if choices and value not in choices:
                return jsonify({'error': f'Invalid value "{value}". Must be one of: {choices}'}), 400
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Invalid value type: {e}'}), 400

    # Apply to config
    try:
        config = load_config()
        if section not in config:
            config[section] = {}
        config[section][key] = value
        save_config(config)
        logger.info(f"AI Butler: applied setting {section}.{key} = {value!r}")
    except Exception as e:
        logger.error(f"AI Butler: failed to save setting: {e}")
        return jsonify({'error': f'Failed to save setting: {e}'}), 500

    # Determine if restart is needed
    ui_only = {'UI Settings', 'Discover Settings', 'Overlay Settings', 'AI Assistant', 'Notifications'}
    requires_restart = section not in ui_only

    return jsonify({'ok': True, 'requires_restart': requires_restart, 'section': section, 'key': key, 'value': value})


@ai_bp.route('/api/ai/restart', methods=['POST'])
def ai_restart():
    """Trigger a program restart (stop + start) after AI-applied settings."""
    from flask_login import current_user as cu
    from routes.utils import is_user_system_enabled
    from routes.program_operation_routes import stop_program, _execute_start_program
    import threading

    if is_user_system_enabled() and not cu.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401

    if not get_setting('AI Assistant', 'enabled', False):
        return jsonify({'error': 'AI Assistant is disabled'}), 403

    if not get_setting('AI Assistant', 'enable_settings_assistant', True):
        return jsonify({'error': 'Settings Assistant (Phase 2) is disabled in AI Assistant settings'}), 403

    def _do_restart():
        import time
        stop_program()
        time.sleep(2)
        _execute_start_program(skip_connectivity_check=True, is_restart=True)

    threading.Thread(target=_do_restart, daemon=True).start()
    logger.info("AI Butler: triggered program restart")
    return jsonify({'ok': True, 'message': 'Restart initiated'})


@ai_bp.route('/api/ai/add_to_library', methods=['POST'])
def ai_add_to_library():
    """Add a title to the wanted list as suggested by the AI Butler.

    Body: { "imdb_id": "tt1234567", "title": "Show Name", "year": 2021, "media_type": "movie|tv" }
    Returns: { "ok": true, "items_added": 5, "title": "Show Name" }

    The imdb_id is preferred. If missing, searches Trakt by title+year.
    """
    from flask_login import current_user as cu
    from routes.utils import is_user_system_enabled
    from utilities.web_scraper import search_trakt
    from cli_battery.app.direct_api import DirectAPI
    from database.wanted_items import add_wanted_items
    from metadata.metadata import process_metadata
    from utilities.settings import load_config as _lc

    # Allow access via session auth (built-in chat) OR tool token auth (OpenClaw/skill)
    tool_auth_ok, _ = _check_tool_auth()
    if is_user_system_enabled() and not cu.is_authenticated and not tool_auth_ok:
        return jsonify({'error': 'Authentication required'}), 401

    if not get_setting('AI Assistant', 'enabled', False):
        return jsonify({'error': 'AI Assistant is disabled'}), 403

    if not get_setting('AI Assistant', 'enable_recommendations', True):
        return jsonify({'error': 'Content Recommendations (Phase 4) is disabled in AI Assistant settings'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    imdb_id = (data.get('imdb_id') or '').strip()
    title = (data.get('title') or '').strip()
    year = data.get('year')
    media_type = (data.get('media_type') or 'movie').lower()
    if media_type not in ('movie', 'tv', 'show'):
        media_type = 'movie'
    if media_type == 'show':
        media_type = 'tv'

    if not imdb_id and not title:
        return jsonify({'error': 'imdb_id or title is required'}), 400

    # Resolution strategy:
    # 1. imdb_id provided → Trakt /search/imdb/{id} → confirmed imdb_id (handles typos/variants)
    # 2. imdb_id lookup fails → fall through to title+year search (handles hallucinated IDs)
    # 3. No imdb_id → title+year search directly
    try:
        needs_title_search = False
        if imdb_id:
            info = _trakt_lookup_by_imdb(imdb_id)
            if info and info.get('imdb_id'):
                confirmed_imdb = info['imdb_id']
                # Cross-check: if user supplied a title, verify Trakt's title loosely matches.
                # This catches hallucinated IDs that happen to resolve to a *different* real film.
                trakt_title = (info.get('title') or '').lower().strip()
                user_title = title.lower().strip() if title else ''
                title_ok = (
                    not user_title  # no title to compare against
                    or trakt_title == user_title
                    or user_title in trakt_title
                    or trakt_title in user_title
                )
                if not title_ok:
                    logger.warning(
                        f"AI add_to_library: Trakt title mismatch — requested '{title}' but "
                        f"{confirmed_imdb} resolves to '{info.get('title')}' — falling back to title search"
                    )
                    needs_title_search = True
                else:
                    if confirmed_imdb != imdb_id:
                        logger.info(f"AI add_to_library: Trakt corrected imdb {imdb_id} → {confirmed_imdb} for '{title}'")
                    else:
                        logger.info(f"AI add_to_library: Trakt confirmed imdb {imdb_id} for '{title}'")
                    imdb_id = confirmed_imdb
                    media_type = info.get('media_type', media_type)
            else:
                logger.warning(f"AI add_to_library: Trakt imdb lookup for {imdb_id} returned nothing — falling back to title search")
                needs_title_search = True
        if not imdb_id or needs_title_search:
            # Fallback 1: MDBList exact title+year match (faster, handles non-English aliases)
            if title:
                mdb = _mdblist_search(title, year, media_type)
                if mdb and mdb.get('imdb_id'):
                    logger.info(f"AI add_to_library: MDBList resolved '{title}' → {mdb['imdb_id']}")
                    imdb_id = mdb['imdb_id']
                    media_type = mdb.get('media_type', media_type)
            # Fallback 2: Trakt title+year text search — exact title match only
            # search_trakt() returns converted dicts: {title, year, media_type, id (tmdb_id), ...}
            if (not imdb_id or needs_title_search) and title:
                results = search_trakt(title, year)
                _trakt_preview = [(r.get('title'), r.get('year')) for r in results[:5]]
                logger.info(f"AI add_to_library: Trakt search '{title}' ({year}) → {len(results)} results: {_trakt_preview}")
                # Normalize apostrophes/quotes for comparison — AI often uses curly quotes
                def _norm(s):
                    return s.lower().replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"')
                title_norm_t = _norm(title)
                year_int_t = int(year) if year else None
                best = None
                for r in results:
                    r_title = _norm(r.get('title') or '')
                    r_year = r.get('year')
                    if r_title == title_norm_t and (not year_int_t or r_year == year_int_t):
                        best = r
                        break
                if best is None:
                    for r in results:
                        r_title = _norm(r.get('title') or '')
                        if r_title == title_norm_t:
                            best = r
                            break
                if best:
                    tmdb_id_str = str(best.get('id', ''))
                    mtype = best.get('media_type', 'movie')
                    resolve_type = 'show' if mtype == 'show' else 'movie'
                    if tmdb_id_str:
                        resolved_imdb, _ = DirectAPI.tmdb_to_imdb(tmdb_id_str, media_type=resolve_type)
                        if resolved_imdb:
                            logger.info(f"AI add_to_library: Trakt title search resolved '{title}' → {resolved_imdb}")
                            imdb_id = resolved_imdb
                            media_type = 'tv' if mtype == 'show' else 'movie'
    except Exception as e:
        logger.warning(f"AI add_to_library: resolution error: {e}")

    if not imdb_id:
        return jsonify({'error': f'Could not resolve IMDB ID for "{title}"'}), 404

    # Use user-selected versions if provided, otherwise use the default (first) version
    selected_versions = data.get('selected_versions')  # list of version names from UI checkboxes
    single_version = data.get('version')  # single version name (from skill/tool API)
    try:
        config = _lc()
        versions_dict = config.get('Scraping', {}).get('versions', {})
        all_versions_list = list(versions_dict.keys()) if versions_dict else ['1080p']
        all_versions = {v: True for v in all_versions_list}
    except Exception:
        all_versions_list = ['1080p']
        all_versions = {'1080p': True}
    if selected_versions and isinstance(selected_versions, list) and len(selected_versions) > 0:
        versions = {v: True for v in selected_versions if v in all_versions}
        if not versions:
            versions = all_versions  # fallback if none matched
    elif single_version and single_version in all_versions:
        versions = {single_version: True}
    else:
        # Default: use only the first configured version (not all versions)
        default_version = all_versions_list[0] if all_versions_list else '1080p'
        versions = {default_version: True}

    # Build wanted item
    wanted_item = {
        'imdb_id': imdb_id,
        'media_type': media_type,
        'title': title,
        'year': year,
        'versions': versions,
    }

    # For TV shows, fetch all seasons
    if media_type == 'tv':
        try:
            seasons_data, _ = DirectAPI.get_show_seasons(imdb_id)
            if seasons_data:
                all_seasons = [int(s) for s in seasons_data.keys() if str(s).isdigit() and int(s) > 0]
                if all_seasons:
                    wanted_item['requested_seasons'] = all_seasons
        except Exception as e:
            logger.debug(f"AI add_to_library: get_show_seasons failed: {e}")

    try:
        # Always prime the battery cache before process_metadata —
        # items not yet in the battery will fail silently without this.
        try:
            DirectAPI.force_refresh_metadata(imdb_id)
        except Exception as e:
            logger.debug(f"AI add_to_library: force_refresh_metadata failed (non-fatal): {e}")

        processed = process_metadata([wanted_item])
        all_items = []
        if processed:
            all_items = processed.get('movies', []) + processed.get('episodes', [])

        # If metadata failed, the imdb_id may be invalid — nothing more we can do here
        if not all_items:
            logger.warning(f"AI add_to_library: process_metadata returned no items for {imdb_id}")

        if not all_items:
            return jsonify({'error': f'Could not retrieve metadata for "{title}" ({imdb_id}). The item may not be in the metadata database yet — try again in a moment.'}), 400

        _butler_name = _get_display_name()
        source_detail = _butler_name
        if is_user_system_enabled() and cu.is_authenticated:
            source_detail = f'{_butler_name} ({cu.username})'
        elif is_user_system_enabled():
            # Tool API call with token — try to resolve username from token
            from routes.auth_routes import User as _User
            _tok = request.args.get('token', '') or (request.headers.get('Authorization', '')[7:] if request.headers.get('Authorization', '').startswith('Bearer ') else '')
            if _tok:
                _u = _User.query.filter_by(api_token=_tok).first()
                if _u:
                    source_detail = f'{_butler_name} ({_u.username})'

        for item in all_items:
            item['content_source'] = 'ai_butler'
            item['content_source_detail'] = source_detail

        items_added = add_wanted_items(all_items, versions)
        logger.info(f"AI Butler: added {items_added} items for {title} ({imdb_id})")
        try:
            from utilities.ai_habits import track_action
            _uid = source_detail
            track_action('library_add_ai', detail=f"{title} ({year}) [{media_type}]", user_id=_uid)
        except Exception:
            pass
        return jsonify({'ok': True, 'items_added': items_added, 'title': title, 'imdb_id': imdb_id})

    except Exception as e:
        logger.error(f"AI add_to_library: failed: {e}", exc_info=True)
        return jsonify({'error': f'Failed to add to library: {e}'}), 500



@ai_bp.route('/api/ai/status', methods=['GET'])
def ai_status():
    """Check if AI Butler is configured and reachable."""
    enabled, openclaw_url, token, agent_id = _get_openclaw_config()

    if not enabled:
        return jsonify({'enabled': False, 'reachable': False, 'message': 'AI Assistant is disabled', 'agent_name': None})

    if not openclaw_url:
        return jsonify({'enabled': True, 'reachable': False, 'message': 'OpenClaw URL not configured', 'agent_name': None})

    http_url = _to_http_url(openclaw_url)
    headers = {}
    if token:
        headers['Authorization'] = f"Bearer {token}"

    agent_name = get_setting('AI Assistant', 'display_name', '') or 'AI Butler'

    try:
        resp = requests.get(
            f"{http_url}/v1/models",
            headers=headers,
            timeout=5
        )
        reachable = resp.status_code in (200, 400, 401, 403, 422)
        message = 'Connected' if reachable else f'HTTP {resp.status_code}'
    except Exception as e:
        reachable = False
        message = str(e)[:100]

    return jsonify({'enabled': True, 'reachable': reachable, 'message': message, 'agent_name': agent_name})



@ai_bp.route('/api/ai/skill_download', methods=['GET'])
def ai_skill_download():
    """Serve the OpenClaw SKILL.md with the user's URL and token pre-filled.

    The user downloads this from the AI Assistant settings page and drops it
    into their OpenClaw ~/.openclaw/skills/ directory.
    """
    import os as _os
    from flask import make_response
    from flask_login import current_user as cu
    from routes.utils import is_user_system_enabled

    enabled, openclaw_url, _openclaw_token, agent_id = _get_openclaw_config()
    if not enabled:
        return jsonify({'error': 'AI Assistant is not enabled'}), 503

    # Determine the CLI_DEBRID_TOKEN to embed in the skill.
    # When user management is enabled: use the current user's personal API token.
    # When user management is disabled: no token needed (endpoints are unauthenticated).
    if is_user_system_enabled():
        if not cu.is_authenticated:
            return jsonify({'error': 'Login required to download skill'}), 401
        cli_debrid_token = getattr(cu, 'api_token', None) or ''
        if not cli_debrid_token:
            return jsonify({'error': 'Your account has no API token. Please regenerate one in User Management.'}), 400
    else:
        cli_debrid_token = ''  # no auth required

    # Determine the public URL of this cli_debrid instance.
    # Priority: 1) SSO redirect_uri_base (already configured for public access), 2) X-Forwarded-Host, 3) request host.
    sso_base = get_setting('SSO', 'redirect_uri_base', '').strip().rstrip('/')
    if sso_base:
        cli_debrid_url = sso_base
    else:
        forwarded_host = request.headers.get('X-Forwarded-Host') or request.host
        forwarded_proto = request.headers.get('X-Forwarded-Proto') or ('https' if request.is_secure else 'http')
        cli_debrid_url = f"{forwarded_proto}://{forwarded_host}"

    skill_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        'static', 'cli_debrid_skill.md'
    )
    try:
        with open(skill_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"AI skill_download: could not read skill template: {e}")
        return jsonify({'error': 'Skill template not found'}), 500

    # Fill in placeholders
    content = content.replace('{{CLI_DEBRID_URL}}', cli_debrid_url)
    content = content.replace('{{CLI_DEBRID_TOKEN}}', cli_debrid_token)

    resp = make_response(content)
    resp.headers['Content-Type'] = 'text/markdown; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="cli_debrid_skill.md"'
    return resp


@ai_bp.route('/api/ai/settings', methods=['GET'])
def ai_get_settings():
    """Return current AI Assistant settings for the in-chat settings modal."""
    keys = [
        'enabled', 'openclaw_url', 'openclaw_token', 'agent_id', 'display_name',
        'health_notifications', 'health_check_interval',
        'enable_settings_assistant', 'enable_proactive_notifications',
        'enable_recommendations', 'enable_habit_tracking', 'share_full_config'
    ]
    data = {k: get_setting('AI Assistant', k) for k in keys}
    data['plex_labels'] = get_setting('AI Assistant', 'plex_labels') or {}
    return jsonify(data)


@ai_bp.route('/api/ai/settings', methods=['POST'])
@admin_required
def ai_save_settings():
    """Save AI Assistant settings from the in-chat settings modal."""
    body = request.get_json(force=True) or {}
    cfg = load_config()
    section = cfg.setdefault('AI Assistant', {})

    bool_keys = [
        'enabled', 'health_notifications',
        'enable_settings_assistant', 'enable_proactive_notifications',
        'enable_recommendations', 'enable_habit_tracking', 'share_full_config'
    ]
    str_keys = ['openclaw_url', 'openclaw_token', 'agent_id', 'display_name']
    int_keys = ['health_check_interval']

    for k in bool_keys:
        if k in body:
            section[k] = bool(body[k])
    for k in str_keys:
        if k in body:
            section[k] = str(body[k]).strip()
    for k in int_keys:
        if k in body:
            try:
                section[k] = int(body[k])
            except (ValueError, TypeError):
                pass

    if 'plex_labels' in body and isinstance(body['plex_labels'], dict):
        pl = body['plex_labels']
        section['plex_labels'] = {
            'enabled': bool(pl.get('enabled', False)),
            'label_mode': str(pl.get('label_mode', 'fixed')),
            'fixed_label': str(pl.get('fixed_label', 'AI Butler')).strip(),
        }

    save_config(cfg)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Phase 6 — OpenClaw Tool API
# These endpoints are designed to be called by OpenClaw tool definitions.
# Auth: Bearer token from AI Assistant settings (same token used for chat).
# All endpoints are read-only except /api/ai/tools/add_to_library.
# ---------------------------------------------------------------------------

def _check_tool_auth():
    """Validate token for tool endpoints. Returns (ok, error_response).

    When the user system is enabled, validates against any user's api_token.
    When the user system is disabled, validates against the OpenClaw token setting.

    Accepts token via:
      - ?token=<token> query parameter
      - Authorization: Bearer <token> header
    """
    from routes.settings_routes import is_user_system_enabled

    # Extract token from request
    incoming = request.args.get('token', '') or ''
    if not incoming:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            incoming = auth[7:]

    if is_user_system_enabled():
        # Validate against any user's api_token
        if not incoming:
            return False, (jsonify({'error': 'API token required'}), 401)
        from routes.auth_routes import User
        user = User.query.filter_by(api_token=incoming).first()
        if not user:
            return False, (jsonify({'error': 'Invalid API token'}), 401)
        return True, None
    else:
        # User system disabled — fall back to OpenClaw token setting
        expected = get_setting('AI Assistant', 'openclaw_token', '')
        if not expected:
            return True, None  # No token configured = open
        if not incoming:
            return False, (jsonify({'error': 'Token required'}), 401)
        if incoming != expected:
            return False, (jsonify({'error': 'Invalid token'}), 401)
        return True, None


@ai_bp.route('/api/ai/tools/queue_status', methods=['GET'])
def tool_queue_status():
    """OpenClaw tool: get current queue counts and program state.

    Example OpenClaw tool definition:
      GET http://<host>/api/ai/tools/queue_status
      Headers: Authorization: Bearer <token>
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err

    try:
        from queues.queue_manager import QueueManager
        from routes.program_operation_routes import program_is_running, get_program_status
        qm = QueueManager()
        counts = {}
        for name in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Collecting',
                     'Upgrading', 'Sleeping', 'Blacklisted']:
            q = getattr(qm, f'{name.lower()}_queue', None)
            if q is not None:
                try:
                    counts[name] = q.qsize()
                except Exception:
                    counts[name] = 0
        return jsonify({
            'running': program_is_running(),
            'status': get_program_status(),
            'queues': counts,
        })
    except Exception as e:
        logger.error(f"AI tool queue_status: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/library_stats', methods=['GET'])
def tool_library_stats():
    """OpenClaw tool: get library counts and recent activity."""
    ok, err = _check_tool_auth()
    if not ok:
        return err

    try:
        from database import get_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM media_items WHERE state IN ('Collected','Upgrading')")
        total_collected = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM media_items WHERE state = 'Wanted'")
        total_wanted = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM media_items WHERE state = 'Blacklisted'")
        total_blacklisted = c.fetchone()[0]
        c.execute("""
            SELECT title, type, collected_at FROM media_items
            WHERE state IN ('Collected','Upgrading')
            AND collected_at >= datetime('now','-1 day')
            ORDER BY collected_at DESC LIMIT 10
        """)
        recently_collected = [{'title': r[0], 'type': r[1], 'collected_at': r[2]}
                               for r in c.fetchall()]
        conn.close()
        return jsonify({
            'collected': total_collected,
            'wanted': total_wanted,
            'blacklisted': total_blacklisted,
            'recently_collected_24h': recently_collected,
        })
    except Exception as e:
        logger.error(f"AI tool library_stats: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/recently_collected', methods=['GET'])
def tool_recently_collected():
    """OpenClaw tool: list recently collected items.

    Query param: limit (default 20, max 50)
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err

    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        from database import get_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT title, year, type, version, collected_at, imdb_id
            FROM media_items
            WHERE state IN ('Collected','Upgrading')
            ORDER BY collected_at DESC LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        items = [{'title': r[0], 'year': r[1], 'type': r[2],
                  'version': r[3], 'collected_at': r[4], 'imdb_id': r[5]}
                 for r in rows]
        return jsonify({'items': items, 'count': len(items)})
    except Exception as e:
        logger.error(f"AI tool recently_collected: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/search_library', methods=['GET'])
def tool_search_library():
    """OpenClaw tool: search the collected library by title.

    Query param: q (search term), type (movie/episode, optional)
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err

    q = request.args.get('q', '').strip()
    mtype = request.args.get('type', '').strip().lower()
    if not q:
        return jsonify({'error': 'q parameter required'}), 400

    try:
        from database import get_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        query = """
            SELECT title, year, type, state, imdb_id, tmdb_id
            FROM media_items
            WHERE title LIKE ?
              AND state IN ('Collected','Upgrading','Wanted')
        """
        params = [f'%{q}%']
        if mtype in ('movie', 'episode'):
            query += ' AND type = ?'
            params.append(mtype)
        query += ' ORDER BY title LIMIT 20'
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        items = [{'title': r[0], 'year': r[1], 'type': r[2],
                  'state': r[3], 'imdb_id': r[4], 'tmdb_id': r[5]} for r in rows]
        return jsonify({'items': items, 'count': len(items), 'query': q})
    except Exception as e:
        logger.error(f"AI tool search_library: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/add_to_library', methods=['POST'])
def tool_add_to_library():
    """OpenClaw tool: add a title to the wanted list (same as /api/ai/add_to_library).

    Body: { "title": "...", "year": 2021, "media_type": "movie|tv", "imdb_id": "tt..." }
    This is a thin alias so OpenClaw tools use a stable /tools/ namespace.
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err
    return ai_add_to_library()


@ai_bp.route('/api/ai/tools/queue_start', methods=['POST'])
def tool_queue_start():
    """OpenClaw tool: start the program/queue processing."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner:
            return jsonify({'error': 'Program runner not initialized'}), 500
        result = runner.start()
        return jsonify({'ok': True, 'message': 'Program start requested', 'result': str(result)})
    except Exception as e:
        logger.error(f"AI tool queue_start: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/queue_stop', methods=['POST'])
def tool_queue_stop():
    """OpenClaw tool: stop the program/queue processing."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner:
            return jsonify({'error': 'Program runner not initialized'}), 500
        result = runner.stop()
        return jsonify({'ok': True, 'message': 'Program stop requested', 'result': str(result)})
    except Exception as e:
        logger.error(f"AI tool queue_stop: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/plex_scan', methods=['POST'])
def tool_plex_scan():
    """OpenClaw tool: trigger a Plex library scan in a background thread."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        import threading, uuid
        from utilities.plex_functions import get_collected_from_plex
        scan_id = str(uuid.uuid4())

        def _run():
            try:
                get_collected_from_plex()
            except Exception as ex:
                logger.error(f"AI tool plex_scan background: {ex}")

        threading.Thread(target=_run, name='ai-plex-scan', daemon=True).start()
        return jsonify({'ok': True, 'message': 'Plex scan started in background', 'scan_id': scan_id})
    except Exception as e:
        logger.error(f"AI tool plex_scan: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/upgrade_scan', methods=['POST'])
def tool_upgrade_scan():
    """OpenClaw tool: trigger an upgrade hub scan."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner:
            return jsonify({'error': 'Program runner not initialized'}), 500
        result = runner.trigger_task('task_upgrade_hub_scan')
        return jsonify({'ok': True, 'message': 'Upgrade scan triggered', 'result': result})
    except Exception as e:
        logger.error(f"AI tool upgrade_scan: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/available_tasks', methods=['GET'])
def tool_available_tasks():
    """OpenClaw tool: list all available APScheduler tasks that can be triggered."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner:
            return jsonify({'error': 'Program runner not initialized'}), 500
        tasks = [job.id for job in runner.scheduler.get_jobs()] if hasattr(runner, 'scheduler') else []
        return jsonify({'tasks': tasks, 'count': len(tasks)})
    except Exception as e:
        logger.error(f"AI tool available_tasks: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/run_task', methods=['POST'])
def tool_run_task():
    """OpenClaw tool: trigger a named APScheduler task.

    Body: { "task_name": "task_heartbeat" }
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err
    data = request.get_json(silent=True) or {}
    task_name = data.get('task_name', '').strip()
    if not task_name:
        return jsonify({'error': 'task_name required'}), 400
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        if not runner:
            return jsonify({'error': 'Program runner not initialized'}), 500
        result = runner.trigger_task(task_name)
        return jsonify({'ok': True, 'message': f'Task {task_name} triggered', 'result': result})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"AI tool run_task: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/trim_memory', methods=['POST'])
def tool_trim_memory():
    """OpenClaw tool: trim process memory via malloc_trim."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        import ctypes
        libc = ctypes.CDLL('libc.so.6')
        libc.malloc_trim(0)
        return jsonify({'ok': True, 'message': 'Memory trim complete'})
    except Exception as e:
        logger.error(f"AI tool trim_memory: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/cleanup_failed_upgrades', methods=['POST'])
def tool_cleanup_failed_upgrades():
    """OpenClaw tool: clean up failed upgrades pkl file."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        import os, pickle
        pkl_path = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'failed_upgrades.pkl')
        if os.path.exists(pkl_path):
            os.remove(pkl_path)
            return jsonify({'ok': True, 'message': 'Failed upgrades cleared'})
        return jsonify({'ok': True, 'message': 'No failed upgrades file found'})
    except Exception as e:
        logger.error(f"AI tool cleanup_failed_upgrades: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/remove_duplicates', methods=['POST'])
def tool_remove_duplicates():
    """OpenClaw tool: remove duplicate items from the database."""
    ok, err = _check_tool_auth()
    if not ok:
        return err
    try:
        import threading

        def _run():
            try:
                from database.database_writing import remove_duplicate_items as _rdi
                _rdi()
            except Exception as ex:
                logger.error(f"AI tool remove_duplicates background: {ex}")

        threading.Thread(target=_run, name='ai-remove-dupes', daemon=True).start()
        return jsonify({'ok': True, 'message': 'Duplicate removal started in background'})
    except Exception as e:
        logger.error(f"AI tool remove_duplicates: {e}")
        return jsonify({'error': str(e)}), 500


def send_ai_notification(message: str, title: str = None) -> bool:
    if title is None:
        title = _get_display_name()
    """
    Shared helper: send a message to all configured external notification channels.
    Returns True if sent successfully, False otherwise.
    Used by both the /api/ai/tools/send_notification endpoint and the health monitor.
    """
    try:
        from routes.notifications import _send_notifications, get_enabled_notifications
        enabled = get_enabled_notifications()
        if not enabled:
            logger.info("send_ai_notification: no notification channels configured/enabled")
            return False
        full_message = f"{title}\n\n{message}" if title else message
        _send_notifications(full_message, enabled, notification_category='program_info')
        logger.info(f"send_ai_notification: sent — {message[:100]}")
        return True
    except Exception as e:
        logger.error(f"send_ai_notification: failed: {e}")
        return False


@ai_bp.route('/api/ai/tools/context', methods=['GET'])
def tool_context():
    """OpenClaw tool: get a rich live snapshot of cli_debrid state.

    Returns queue counts, library stats, recent errors, upgrade hub activity,
    recent notifications, and statistics — similar to the built-in chat context.

    GET /api/ai/tools/context?token=<token>
    """
    ok, err = _check_tool_auth()
    if not ok:
        return err

    try:
        from utilities.ai_context import (
            _get_queue_state, _get_library_stats, _get_recent_logs,
            _get_statistics_summary, _get_upgrade_hub_activity,
            _get_notifications_log, _get_program_uptime,
            _get_collected_library, _get_watch_history,
        )
        from routes.program_operation_routes import program_is_running

        # Optional expansions via query params
        include_library = request.args.get('library', 'false').lower() == 'true'
        include_history = request.args.get('history', 'false').lower() == 'true'

        queue_state   = _get_queue_state()
        lib_stats     = _get_library_stats()
        uptime        = _get_program_uptime()
        stats         = _get_statistics_summary()
        error_count, recent_errors, last_warning, log_tail = _get_recent_logs()
        upgrade_activity = _get_upgrade_hub_activity()
        notifications = _get_notifications_log()

        result = {
            'ok': True,
            'running': program_is_running(),
            'uptime': uptime,
            'queues': queue_state,
            'library': lib_stats,
            'statistics': stats,
            'errors': {
                'count': error_count,
                'last_warning': last_warning,
                'recent': recent_errors,
            },
            'log_tail': log_tail,
            'upgrade_hub': upgrade_activity,
            'notifications': notifications,
        }

        if include_library:
            result['collected_library'] = _get_collected_library()
        if include_history:
            result['watch_history'] = _get_watch_history()

        return jsonify(result)
    except Exception as e:
        logger.error(f"AI tool context: {e}")
        return jsonify({'error': str(e)}), 500


@ai_bp.route('/api/ai/tools/send_notification', methods=['POST'])
def tool_send_notification():
    """
    OpenClaw tool: send a message to the user's configured external notification channels
    (Discord, Telegram, Apprise, etc.). Use this when the user asks you to send them a
    notification or alert about something — queue status, scan results, reminders, etc.

    Body (JSON):
      message  — required, the notification body text
      title    — optional, defaults to "AI Butler"
    """
    # Allow both: OpenClaw tool calls (Bearer token) and in-chat widget (session auth)
    from routes.utils import is_user_system_enabled
    from flask_login import current_user as _cu
    if is_user_system_enabled() and not _cu.is_authenticated:
        ok, err = _check_tool_auth()
        if not ok:
            return err
    if not get_setting('AI Assistant', 'enabled', False):
        return jsonify({'error': 'AI Assistant is disabled'}), 403
    try:
        data = request.get_json(silent=True) or {}
        message = (data.get('message') or '').strip()
        title = (data.get('title') or _get_display_name()).strip()
        if not message:
            return jsonify({'error': 'message is required'}), 400
        sent = send_ai_notification(message, title=title)
        if sent:
            return jsonify({'ok': True, 'message': 'Notification sent'})
        else:
            return jsonify({'ok': False, 'message': 'No notification channels are configured or enabled'}), 200
    except Exception as e:
        logger.error(f"AI tool send_notification: {e}")
        return jsonify({'error': str(e)}), 500
