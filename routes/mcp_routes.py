"""
MCP endpoint (read-only; see utilities/mcp_server.py) and its setup page.

POST /mcp        JSON-RPC from an MCP client; needs the MCP token
                 (Authorization: Bearer <token>) and MCP turned on, whatever
                 the user system is set to.
GET  /mcp/setup  admin page: turn it on/off, the URL, token and client config.
"""

import hmac
import json
import logging
import os
import secrets

from flask import Blueprint, Response, jsonify, render_template, request

from routes.models import admin_required
from utilities.settings import get_setting, set_setting

mcp_bp = Blueprint('mcp', __name__)
SECTION = 'MCP'
MAX_BODY = 1 << 20


def _enabled() -> bool:
    return bool(get_setting(SECTION, 'enabled', False))


def _token() -> str:
    return str(get_setting(SECTION, 'token', '') or '')


def _authorized() -> bool:
    want = _token()
    if not want:
        return False
    auth = request.headers.get('Authorization', '')
    got = auth[7:].strip() if auth.startswith('Bearer ') else ''
    return bool(got) and hmac.compare_digest(got.encode(), want.encode())


def _version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'version.txt')) as f:
            return f.read().strip()
    except OSError:
        return ''


@mcp_bp.route('', methods=['POST', 'GET', 'DELETE'])
def endpoint():
    if request.method != 'POST':
        # Stateless JSON-only server: no SSE stream, no sessions to end.
        return Response(status=405, headers={'Allow': 'POST'})
    if not _enabled():
        return jsonify({'error': 'MCP is turned off (Settings → MCP)'}), 404
    if not _authorized():
        return jsonify({'error': 'invalid or missing MCP token'}), 401, {'WWW-Authenticate': 'Bearer'}
    if (request.content_length or 0) > MAX_BODY:
        return jsonify({'error': 'request too large'}), 413
    from utilities import mcp_server
    try:
        body = json.loads(request.get_data(as_text=True) or 'null')
    except ValueError:
        return jsonify(mcp_server._error(None, -32700, 'Parse error')), 400
    version = _version()
    if isinstance(body, list):
        out = [r for r in (mcp_server.handle(m, version) for m in body) if r is not None]
    else:
        out = mcp_server.handle(body, version)
    if not out:
        return Response(status=202)  # only notifications
    return jsonify(out)


def _public_url() -> str:
    return request.host_url.rstrip('/') + '/mcp'


@mcp_bp.route('/setup')
@admin_required
def setup():
    return render_template('mcp_setup.html')


@mcp_bp.route('/setup/api', methods=['GET', 'POST'])
@admin_required
def setup_api():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if 'enabled' in data:
            set_setting(SECTION, 'enabled', bool(data['enabled']))
            if data['enabled'] and not _token():
                set_setting(SECTION, 'token', secrets.token_urlsafe(32))
        if data.get('new_token'):
            set_setting(SECTION, 'token', secrets.token_urlsafe(32))
            logging.info('[MCP] new token generated; clients must be updated')
    url, token = _public_url(), _token()
    return jsonify({
        'enabled': _enabled(), 'url': url, 'token': token,
        'claude_code': f'claude mcp add --transport http cli_debrid {url} --header "Authorization: Bearer {token}"',
        'json_config': {'mcpServers': {'cli_debrid': {'type': 'http', 'url': url,
                                                      'headers': {'Authorization': f'Bearer {token}'}}}},
    })
