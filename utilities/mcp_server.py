"""
Read-only MCP (Model Context Protocol) server for cli_debrid.

An AI assistant (Claude Desktop / Claude Code, or any MCP client) connects to
POST /mcp with the MCP token and can look at cli_debrid: explain settings,
read logs, check queues, the mount and connections, and look up an item. It
can't change anything: every tool here only reads.

Transport: MCP "Streamable HTTP", JSON responses only (no server-initiated
messages), stateless. Every tool result passes through the log redactor
(utilities.log_redaction.scrub) and secret-looking keys are masked, so API
keys, tokens and passwords never leave cli_debrid.
"""

import json
import logging
import os
import re
from collections import deque
from typing import Any, Dict, List, Optional

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
SERVER_NAME = 'cli_debrid'
INSTRUCTIONS = (
    "Read-only access to a cli_debrid install (media automation: watchlists -> scrapers -> debrid/usenet -> "
    "library/symlinks -> Plex/Jellyfin). Start with get_overview. Use explain_setting/list_settings to explain "
    "settings, search_logs/recent_errors to diagnose problems, lookup_item to follow one movie or episode. "
    "To help report a bug, use prepare_issue_report; for how a feature works, get_help. "
    "Secrets are always masked. Nothing can be changed through this server: tell the user which setting to change "
    "and where (Settings page section/key, or the Mount page for 'Built-in Mount')."
)

_SENSITIVE_FRAGMENTS = ('token', 'secret', 'password', 'passwd', 'api_key', 'apikey', 'credential',
                        'private_key', 'webhook', 'bearer', 'cookie', 'proxy', 'download_api_keys')
_SENSITIVE_EXACT = {'key', 'auth', 'username', 'user', 'email', 'pass'}
MASK_SET, MASK_UNSET = '*** (set)', '(not set)'


# ---- redaction ---------------------------------------------------------------

def _sensitive(key: str, spec: Optional[dict] = None) -> bool:
    if spec and spec.get('sensitive'):
        return True
    k = str(key).lower()
    return k in _SENSITIVE_EXACT or any(f in k for f in _SENSITIVE_FRAGMENTS)


def redact(obj: Any, _depth: int = 0) -> Any:
    """Mask secret-looking keys anywhere in dicts, lists and JSON strings."""
    if _depth > 12:
        return '...'
    if isinstance(obj, dict):
        return {k: ((MASK_SET if v not in (None, '', [], {}) else MASK_UNSET) if _sensitive(k)
                    else redact(v, _depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, str) and obj[:1] in '[{':
        try:
            return redact(json.loads(obj), _depth + 1)
        except ValueError:
            return obj
    return obj


def _scrub(text: str) -> str:
    try:
        from utilities.log_redaction import scrub
        return scrub(text)
    except Exception:
        return text


# ---- tools --------------------------------------------------------------------

def _get_setting(*a, **kw):
    from utilities.settings import get_setting
    return get_setting(*a, **kw)


def _schema() -> dict:
    from utilities.settings_schema import SETTINGS_SCHEMA
    return SETTINGS_SCHEMA


def _section_items(section: dict) -> dict:
    items = section.get('schema', section) if isinstance(section, dict) else {}
    return {k: v for k, v in items.items() if isinstance(v, dict) and k != 'tab'}


def _describe(spec: dict) -> str:
    d = spec.get('description', '')
    return ' '.join(d) if isinstance(d, list) else str(d or '')


def _setting_row(section: str, key: str, spec: dict, current: dict) -> dict:
    val = current.get(key, spec.get('default'))
    if _sensitive(key, spec):
        val = MASK_SET if val not in (None, '') else MASK_UNSET
    else:
        val = redact(val)
    # 'setting', not 'key': the log scrubber masks any "key": "..." pair
    row = {'setting': key, 'value': val, 'type': spec.get('type'), 'description': _describe(spec)}
    if 'default' in spec and not _sensitive(key, spec):
        row['default'] = spec['default']
    if spec.get('choices'):
        row['choices'] = spec['choices']
    return row


def tool_list_settings(section: str = '') -> dict:
    schema = _schema()
    if not section:
        return {'sections': sorted(schema), 'hint': 'Call again with section=<name> for its settings.'}
    match = next((s for s in schema if s.lower() == section.lower()), None)
    if match is None:
        return {'error': f'no settings section {section!r}', 'sections': sorted(schema)}
    current = _get_setting(match) or {}
    current = current if isinstance(current, dict) else {}
    return {'section': match, 'settings': [_setting_row(match, k, spec, current)
                                           for k, spec in _section_items(schema[match]).items()]}


def tool_explain_setting(section: str, key: str) -> dict:
    out = tool_list_settings(section)
    if 'error' in out:
        return out
    row = next((r for r in out['settings'] if r['setting'].lower() == str(key).lower()), None)
    if row is None:
        return {'error': f'no setting {key!r} in {out["section"]}', 'settings': [r['setting'] for r in out['settings']]}
    return dict(row, section=out['section'])


def _program_state() -> dict:
    try:
        from routes.program_operation_routes import program_is_running, get_program_status
        return {'running': bool(program_is_running()), 'status': get_program_status()}
    except Exception as e:
        return {'error': str(e)}


def tool_queue_status() -> dict:
    out = _program_state()
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            out['items_by_state'] = {r[0]: r[1] for r in conn.execute(
                "SELECT state, COUNT(*) FROM media_items GROUP BY state ORDER BY 2 DESC")}
        finally:
            conn.close()
    except Exception as e:
        out['items_error'] = str(e)
    return out


def tool_mount_status() -> dict:
    try:
        from utilities import builtin_engine
        settings = {'Usenet Provider': _get_setting('Usenet Provider') or {},
                    'Built-in Mount': _get_setting('Built-in Mount') or {}}
        if not builtin_engine.selected(settings):
            return {'built_in_mount': False, 'usenet_provider': settings['Usenet Provider'].get('provider'),
                    'note': 'The built-in mount is not in use; the mount is an external tool.'}
        out = {'built_in_mount': True, 'state': builtin_engine.state(),
               'stopped_by_user': builtin_engine.stopped_by_user(), 'problems': builtin_engine.problems(settings)}
        from usenet.builtin_client import BuiltinClient
        c = BuiltinClient()
        code, body = c._call('GET', '/status', timeout=5)
        if code == 200 and isinstance(body, dict):
            out['engine'] = body
        code, body = c._call('GET', '/stats', timeout=5)
        if code == 200 and isinstance(body, dict):
            out['stats'] = body
        out['open_files'] = [{k: s.get(k) for k in ('folder', 'file', 'protocol', 'bytes_read', 'rate_bytes', 'opened_at')}
                             for s in (c.get_streams() or [])]
        return out
    except Exception as e:
        return {'error': f'mount status failed: {e}'}


def tool_connection_health() -> dict:
    try:
        from utilities import connection_health
        return connection_health.summary()
    except Exception as e:
        return {'error': str(e)}


def tool_get_overview() -> dict:
    fm = _get_setting('File Management') or {}
    version = ''
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'version.txt')) as f:
            version = f.read().strip()
    except OSError:
        pass
    return {
        'version': version,
        'program': _program_state(),
        'library_mode': fm.get('file_collection_management'),
        'media_server': fm.get('media_server_type') or 'plex',
        'debrid_provider': (_get_setting('Debrid Provider') or {}).get('provider'),
        'usenet_provider': (_get_setting('Usenet Provider') or {}).get('provider'),
        'connections': tool_connection_health(),
        'queues': tool_queue_status().get('items_by_state'),
    }


_LEVELS = {'debug': 10, 'info': 20, 'warning': 30, 'warn': 30, 'error': 40, 'critical': 50}
_MOUNT_LEVEL = re.compile(r'\|\s*(DEBUG|INFO|WARN|WARNING|ERROR)\s*\|')


def _app_log_entries(max_lines: int) -> List[dict]:
    from routes.log_viewer_routes import get_recent_logs
    return [{'time': e['timestamp'], 'level': e['level'], 'message': e['message']}
            for e in get_recent_logs(max_lines)]


def _mount_log_entries(max_lines: int) -> List[dict]:
    from usenet.builtin_client import engine_log_path, format_engine_log_line
    tail = deque(maxlen=max_lines)
    try:
        with open(engine_log_path(), encoding='utf-8', errors='replace') as f:
            for raw in f:
                if raw.strip():
                    tail.append(format_engine_log_line(raw.rstrip('\n')))
    except FileNotFoundError:
        return []
    out = []
    for line in tail:
        m = _MOUNT_LEVEL.search(line)
        out.append({'time': line[:23], 'level': (m.group(1).lower() if m else 'info'), 'message': line})
    return out


def tool_search_logs(query: str = '', level: str = 'all', source: str = 'app', limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 300))
    source = (source or 'app').lower()
    entries = _mount_log_entries(20000) if source == 'mount' else _app_log_entries(20000)
    floor = _LEVELS.get((level or 'all').lower(), 0)
    q = (query or '').lower()
    hits = [e for e in entries
            if _LEVELS.get(e['level'], 20) >= floor and (not q or q in e['message'].lower())]
    return {'source': source, 'query': query, 'level': level, 'matches': len(hits), 'entries': hits[-limit:],
            'note': 'Newest last. Searches the most recent 20000 log lines.'}


def tool_recent_errors(limit: int = 50, source: str = 'app') -> dict:
    return tool_search_logs('', 'warning', source, limit)


_ITEM_COLS = ('id', 'title', 'year', 'type', 'season_number', 'episode_number', 'episode_title', 'state',
              'version', 'imdb_id', 'tmdb_id', 'filled_by_title', 'filled_by_file', 'location_on_disk',
              'collected_at', 'last_updated', 'blacklisted_date', 'upgrading_from')


def tool_lookup_item(query: str, season: Optional[int] = None, episode: Optional[int] = None,
                     limit: int = 20, with_logs: bool = True) -> dict:
    """A movie/episode by title or IMDb id: its state and files, plus recent log lines about it."""
    from database.core import get_db_connection
    limit = max(1, min(int(limit or 20), 50))
    q = (query or '').strip()
    if not q:
        return {'error': 'query is required (title or IMDb id like tt0133093)'}
    conn = get_db_connection()
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(media_items)')]
        use = [c for c in _ITEM_COLS if c in cols]
        if not use:
            return {'error': 'the library database has no items yet'}
        where, params = ('imdb_id = ?', [q]) if re.fullmatch(r'tt\d+', q) else ('title LIKE ?', [f'%{q}%'])
        if season is not None:
            where += ' AND season_number = ?'
            params.append(int(season))
        if episode is not None:
            where += ' AND episode_number = ?'
            params.append(int(episode))
        rows = conn.execute(f"SELECT {', '.join(use)} FROM media_items WHERE {where} "
                            f"ORDER BY title, season_number, episode_number LIMIT ?", params + [limit]).fetchall()
        items = [dict(zip(use, r)) for r in rows]
    finally:
        conn.close()
    out = {'query': q, 'items': items}
    if with_logs and items:
        title = items[0]['title'] or q
        out['recent_log_lines'] = tool_search_logs(title, 'all', 'app', 30)['entries']
    return out


_HELP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'help_content')
DOCS_URL = 'https://godver3.github.io/cli_docs/'
DISCORD_URL = 'https://discord.gg/ynqnXGJ4hU'
ISSUES_URL = 'https://github.com/godver3/cli_debrid/issues'


def _help_topics() -> Dict[str, str]:
    """topic -> first heading of each built-in help page."""
    out = {}
    try:
        names = sorted(n for n in os.listdir(_HELP_DIR) if n.endswith('.md'))
    except OSError:
        return out
    for n in names:
        title = ''
        try:
            with open(os.path.join(_HELP_DIR, n), encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('#'):
                        title = line.strip('# \n')
                        break
        except OSError:
            continue
        out[n[:-3]] = title
    return out


def tool_get_help(topic: str = '') -> dict:
    """The in-app help pages (the same text the "?" help panel shows)."""
    topics = _help_topics()
    if not topic:
        return {'topics': topics, 'docs': DOCS_URL,
                'hint': 'Call again with topic=<name>. The online docs cover setup and every setting in more depth.'}
    name = re.sub(r'[^a-z0-9_]', '', str(topic).lower().replace('-', '_').replace(' ', '_'))
    if name not in topics:
        close = [t for t in topics if name and (name in t or t in name)]
        return {'error': f'no help topic {topic!r}', 'did_you_mean': close or sorted(topics)}
    with open(os.path.join(_HELP_DIR, name + '.md'), encoding='utf-8') as f:
        return {'topic': name, 'text': f.read()[:60000], 'docs': DOCS_URL}


REPORT_TIPS = [
    'Say what you did, what you expected and what happened instead; for one movie/episode, give its title (and S/E).',
    'Include the time it happened (with timezone) so the matching log lines can be found.',
    'Attach logs: Logs page -> "Share cli_debrid Logs" (and the Mount tab\'s share button if streaming or playback '
    'is involved). Sharing uploads the redacted logs plus a settings snapshot to paste.c-net.org and gives a link '
    'to paste into the report. This assistant cannot upload anything itself.',
    'Share soon after it happens: the shared logs are the most recent lines. For mount/playback problems turn on '
    'Mount page -> Advanced -> Debug logging, reproduce, then share (cli_debrid\'s own log already includes debug lines).',
    'Never paste API keys, tokens or passwords; the shared logs and this report already mask them.',
    f'Ask in Discord ({DISCORD_URL}) first for setup questions; file bugs at {ISSUES_URL}.',
]


def tool_prepare_issue_report(description: str = '', item: str = '') -> dict:
    """Everything a bug report needs, in one go (read-only; nothing is sent)."""
    out = {'tips': REPORT_TIPS, 'where': {'discord': DISCORD_URL, 'github_issues': ISSUES_URL, 'docs': DOCS_URL},
           'description': description, 'overview': tool_get_overview()}
    try:
        from utilities.ai_context import get_diagnostic_settings_snapshot
        out['settings_snapshot'] = get_diagnostic_settings_snapshot()
    except Exception as e:
        out['settings_snapshot'] = f'unavailable: {e}'
    out['recent_app_problems'] = tool_search_logs('', 'warning', 'app', 40)['entries']
    if tool_mount_status().get('built_in_mount'):
        out['recent_mount_problems'] = tool_search_logs('', 'warning', 'mount', 40)['entries']
    if item:
        out['item'] = tool_lookup_item(item, limit=5)
    out['draft'] = (
        '**What happened**\n' + (description or '<describe the problem>') + '\n\n'
        '**Expected**\n<what you expected>\n\n'
        '**When**\n<date/time and timezone>\n\n'
        f"**Version / setup**\ncli_debrid {out['overview'].get('version')}, "
        f"{out['overview'].get('library_mode')}, media server {out['overview'].get('media_server')}, "
        f"debrid {out['overview'].get('debrid_provider') or 'none'}, usenet {out['overview'].get('usenet_provider') or 'none'}\n\n"
        '**Logs**\n<link from Logs page -> Share>')
    return out


# ---- registry -----------------------------------------------------------------

def _obj(props: dict, required: Optional[List[str]] = None) -> dict:
    return {'type': 'object', 'properties': props, 'required': required or [], 'additionalProperties': False}


TOOLS: Dict[str, Dict[str, Any]] = {
    'get_overview': {
        'fn': tool_get_overview,
        'description': 'Version, whether the program is running, library mode, providers, connection health '
                       'and item counts per state. Start here.',
        'schema': _obj({})},
    'list_settings': {
        'fn': tool_list_settings,
        'description': 'Without section: the settings sections. With section: every setting in it with its current '
                       'value (secrets masked), default, choices and description.',
        'schema': _obj({'section': {'type': 'string', 'description': 'e.g. "Scraping", "Built-in Mount"'}})},
    'explain_setting': {
        'fn': tool_explain_setting,
        'description': 'One setting: what it does, its current value (secrets masked), default and choices.',
        'schema': _obj({'section': {'type': 'string'}, 'key': {'type': 'string'}}, ['section', 'key'])},
    'search_logs': {
        'fn': tool_search_logs,
        'description': 'Search recent log lines (case-insensitive text match). source "app" = cli_debrid, '
                       '"mount" = the built-in mount engine. level is the minimum level.',
        'schema': _obj({'query': {'type': 'string'},
                        'level': {'type': 'string', 'enum': ['all', 'debug', 'info', 'warning', 'error']},
                        'source': {'type': 'string', 'enum': ['app', 'mount']},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 300}})},
    'recent_errors': {
        'fn': tool_recent_errors,
        'description': 'The most recent warnings and errors.',
        'schema': _obj({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 300},
                        'source': {'type': 'string', 'enum': ['app', 'mount']}})},
    'queue_status': {
        'fn': tool_queue_status,
        'description': 'Program running state and how many items are in each state (Wanted, Scraping, Checking...).',
        'schema': _obj({})},
    'mount_status': {
        'fn': tool_mount_status,
        'description': 'The built-in mount: engine state, problems, servers, cache, open files.',
        'schema': _obj({})},
    'connection_health': {
        'fn': tool_connection_health,
        'description': 'Which services (debrid, Plex, scrapers, content sources...) are reachable.',
        'schema': _obj({})},
    'get_help': {
        'fn': tool_get_help,
        'description': "cli_debrid's built-in help pages (what each page and feature does, tips). Without topic: "
                       'the list of topics. Use it to explain features and suggest fixes.',
        'schema': _obj({'topic': {'type': 'string', 'description': 'e.g. "queues", "debrid_manager", "logs_logs"'}})},
    'prepare_issue_report': {
        'fn': tool_prepare_issue_report,
        'description': 'Gathers what a bug report needs: version and setup, the redacted settings snapshot, '
                       'recent warnings/errors (app and mount), the item involved, reporting tips and a draft '
                       'report. Nothing is sent anywhere; the user shares logs from the Logs page.',
        'schema': _obj({'description': {'type': 'string', 'description': "the user's own words about the problem"},
                        'item': {'type': 'string', 'description': 'title or IMDb id of the movie/show involved'}})},
    'lookup_item': {
        'fn': tool_lookup_item,
        'description': 'Find movies/episodes by title or IMDb id: state, version, the release and file it was '
                       'filled with, where it is on disk, plus recent log lines mentioning it.',
        'schema': _obj({'query': {'type': 'string'}, 'season': {'type': 'integer'}, 'episode': {'type': 'integer'},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                        'with_logs': {'type': 'boolean'}}, ['query'])},
}


def _result(id_, result) -> dict:
    return {'jsonrpc': '2.0', 'id': id_, 'result': result}


def _error(id_, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': code, 'message': message}}


def call_tool(name: str, arguments: Optional[dict]) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {'content': [{'type': 'text', 'text': f'Unknown tool {name!r}'}], 'isError': True}
    args = arguments if isinstance(arguments, dict) else {}
    allowed = set(tool['schema']['properties'])
    try:
        out = tool['fn'](**{k: v for k, v in args.items() if k in allowed})
        is_error = isinstance(out, dict) and 'error' in out and len(out) <= 3
    except TypeError as e:
        out, is_error = {'error': f'bad arguments: {e}'}, True
    except Exception as e:
        logging.warning(f'[MCP] tool {name} failed: {e}')
        out, is_error = {'error': str(e)}, True
    text = _scrub(json.dumps(redact(out), default=str, indent=1))
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


def handle(message: Any, version: str = '') -> Optional[dict]:
    """One JSON-RPC message -> response (None for notifications)."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or 'method' not in message:
        return _error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid Request')
    id_, method, params = message.get('id'), message['method'], message.get('params') or {}
    if id_ is None:
        return None  # notification (notifications/initialized, cancelled...)
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return _result(id_, {
            'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': version or '0'},
            'instructions': INSTRUCTIONS})
    if method == 'ping':
        return _result(id_, {})
    if method == 'tools/list':
        return _result(id_, {'tools': [{'name': n, 'description': t['description'], 'inputSchema': t['schema'],
                                        'annotations': {'readOnlyHint': True, 'openWorldHint': False}}
                                       for n, t in TOOLS.items()]})
    if method == 'tools/call':
        return _result(id_, call_tool(params.get('name'), params.get('arguments')))
    if method in ('resources/list', 'prompts/list'):
        return _result(id_, {method.split('/')[0]: []})
    return _error(id_, -32601, f'Method not found: {method}')
