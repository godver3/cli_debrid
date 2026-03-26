"""
OIDC / SSO authentication routes.

Supports Authentik, Authelia, and any generic OpenID Connect provider.
Authentication flow:
  1. GET /auth/oidc/login  — build authorization URL, redirect to provider
  2. GET /auth/oidc/callback — exchange code for tokens, login or provision user

Token is passed via PKCE + state cookie for CSRF protection.
No external OIDC library required — uses only `requests` (already a dependency).
"""

import secrets
import hashlib
import base64
import logging
import requests as http
from urllib.parse import urlencode

from flask import Blueprint, redirect, request, session, url_for, flash, make_response, jsonify
from flask_login import login_user

from routes.extensions import db
from routes.auth_routes import User, _generate_api_token
from utilities.settings import get_setting

oidc_bp = Blueprint('oidc', __name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_oidc_config():
    """Return OIDC settings dict. Returns None if SSO is disabled or misconfigured."""
    enabled = get_setting('SSO', 'enabled', False)
    if not enabled:
        return None
    discovery_url = get_setting('SSO', 'discovery_url', '').strip()
    client_id = get_setting('SSO', 'client_id', '').strip()
    client_secret = get_setting('SSO', 'client_secret', '').strip()
    if not discovery_url or not client_id:
        return None
    return {
        'discovery_url': discovery_url,
        'client_id': client_id,
        'client_secret': client_secret,
        'provider': get_setting('SSO', 'provider', 'generic'),
        'default_role': get_setting('SSO', 'default_role', 'user'),
        'auto_provision': get_setting('SSO', 'auto_provision', True),
    }


_discovery_cache = {}  # url -> metadata dict

def _fetch_discovery(discovery_url):
    """Fetch and cache OIDC discovery document."""
    if discovery_url not in _discovery_cache:
        try:
            resp = http.get(discovery_url, timeout=10)
            resp.raise_for_status()
            _discovery_cache[discovery_url] = resp.json()
        except Exception as e:
            logger.error(f"OIDC: failed to fetch discovery document from {discovery_url}: {e}")
            return None
    return _discovery_cache.get(discovery_url)


def _pkce_pair():
    """Generate a PKCE code_verifier + code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge


def _callback_uri():
    """Build the redirect_uri for the callback endpoint.

    Uses the configured redirect_uri_base if set (needed when behind a reverse
    proxy that doesn't forward X-Forwarded-Host correctly). Falls back to
    Flask's url_for with _external=True which reads the request host.
    """
    base = get_setting('SSO', 'redirect_uri_base', '').strip().rstrip('/')
    if base:
        return base + '/auth/oidc/callback'
    return url_for('oidc.callback', _external=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@oidc_bp.route('/login')
def login():
    """Redirect user to the OIDC provider's authorization endpoint."""
    cfg = _get_oidc_config()
    if not cfg:
        flash('SSO is not configured or disabled.', 'error')
        return redirect(url_for('auth.login'))

    meta = _fetch_discovery(cfg['discovery_url'])
    if not meta:
        flash('Could not reach SSO provider. Please try again later.', 'error')
        return redirect(url_for('auth.login'))

    authorization_endpoint = meta.get('authorization_endpoint')
    if not authorization_endpoint:
        flash('SSO provider discovery document is missing authorization_endpoint.', 'error')
        return redirect(url_for('auth.login'))

    # PKCE
    verifier, challenge = _pkce_pair()
    # State for CSRF
    state = secrets.token_urlsafe(32)

    # Store in session (short-lived, cleared after callback)
    session['oidc_state'] = state
    session['oidc_verifier'] = verifier
    session['oidc_next'] = request.args.get('next', '')

    params = {
        'response_type': 'code',
        'client_id': cfg['client_id'],
        'redirect_uri': _callback_uri(),
        'scope': 'openid email profile',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }

    auth_url = authorization_endpoint + '?' + urlencode(params)
    logger.info(f"OIDC: redirecting to provider ({cfg['provider']})")
    return redirect(auth_url)


@oidc_bp.route('/callback')
def callback():
    """Handle the authorization code callback from the OIDC provider."""
    cfg = _get_oidc_config()
    if not cfg:
        flash('SSO is not configured or disabled.', 'error')
        return redirect(url_for('auth.login'))

    # CSRF check
    returned_state = request.args.get('state', '')
    expected_state = session.pop('oidc_state', None)
    if not expected_state or returned_state != expected_state:
        logger.warning("OIDC: state mismatch — possible CSRF attempt")
        flash('SSO login failed: invalid state. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    error = request.args.get('error')
    if error:
        desc = request.args.get('error_description', error)
        logger.warning(f"OIDC: provider returned error: {desc}")
        flash(f'SSO login failed: {desc}', 'error')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        flash('SSO login failed: no authorization code received.', 'error')
        return redirect(url_for('auth.login'))

    verifier = session.pop('oidc_verifier', None)
    next_url = session.pop('oidc_next', '') or url_for('root.root')

    meta = _fetch_discovery(cfg['discovery_url'])
    if not meta:
        flash('Could not reach SSO provider. Please try again later.', 'error')
        return redirect(url_for('auth.login'))

    token_endpoint = meta.get('token_endpoint')
    userinfo_endpoint = meta.get('userinfo_endpoint')

    # Exchange code for tokens
    try:
        token_data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': _callback_uri(),
            'client_id': cfg['client_id'],
        }
        if cfg['client_secret']:
            token_data['client_secret'] = cfg['client_secret']
        if verifier:
            token_data['code_verifier'] = verifier

        token_resp = http.post(token_endpoint, data=token_data, timeout=15)
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except Exception as e:
        logger.error(f"OIDC: token exchange failed: {e}")
        flash('SSO login failed: could not exchange authorization code.', 'error')
        return redirect(url_for('auth.login'))

    access_token = tokens.get('access_token')
    if not access_token:
        flash('SSO login failed: no access token returned.', 'error')
        return redirect(url_for('auth.login'))

    # Fetch userinfo
    try:
        userinfo_resp = http.get(
            userinfo_endpoint,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except Exception as e:
        logger.error(f"OIDC: userinfo fetch failed: {e}")
        flash('SSO login failed: could not retrieve user information.', 'error')
        return redirect(url_for('auth.login'))

    logger.info(f"OIDC: userinfo claims: {list(userinfo.keys())} | preferred_username={userinfo.get('preferred_username')!r} | name={userinfo.get('name')!r} | email={userinfo.get('email')!r} | nickname={userinfo.get('nickname')!r}")

    sub = userinfo.get('sub')
    email = userinfo.get('email', '')
    # Derive a clean username: prefer email prefix over full email/name
    _pref = userinfo.get('preferred_username', '')
    if _pref and '@' in _pref:
        _pref = _pref.split('@')[0]  # e.g. mash2k3@gmail.com -> mash2k3
    preferred_username = (
        _pref
        or (email.split('@')[0] if email else None)
        or userinfo.get('name')
        or sub
    )

    if not sub:
        flash('SSO login failed: provider did not return a subject claim.', 'error')
        return redirect(url_for('auth.login'))

    # 1. Look up by oidc_sub (returning SSO user)
    user = User.query.filter_by(oidc_sub=sub).first()

    # 2. Look up by email (link existing local account)
    if not user and email:
        user = User.query.filter_by(email=email).first()
        if user:
            user.oidc_sub = sub
            user.oidc_provider = cfg['provider']
            db.session.commit()
            logger.info(f"OIDC: linked SSO to existing account '{user.username}' via email")

    # 3. Look up by preferred_username matching cli_debrid username
    #    Covers the common case where Authentik username == cli_debrid username
    if not user and preferred_username:
        user = User.query.filter_by(username=preferred_username).first()
        if user:
            user.oidc_sub = sub
            user.oidc_provider = cfg['provider']
            if email and not user.email:
                user.email = email
            db.session.commit()
            logger.info(f"OIDC: linked SSO to existing account '{user.username}' via preferred_username")

    # 4. Look up by email-prefix (e.g. mash2k3@gmail.com -> mash2k3)
    #    Handles providers where preferred_username is set to the email address
    if not user and email and '@' in email:
        email_prefix = email.split('@')[0]
        user = User.query.filter_by(username=email_prefix).first()
        if user:
            user.oidc_sub = sub
            user.oidc_provider = cfg['provider']
            if not user.email:
                user.email = email
            db.session.commit()
            logger.info(f"OIDC: linked SSO to existing account '{user.username}' via email prefix")

    # 3. Auto-provision new user
    if not user:
        if not cfg['auto_provision']:
            logger.warning(f"OIDC: no account for sub={sub}, auto-provision disabled")
            flash('SSO login failed: no matching account found. Contact an administrator.', 'error')
            return redirect(url_for('auth.login'))

        # Ensure username is unique
        base_username = preferred_username
        username = base_username
        counter = 1
        while User.query.filter_by(username=username).first():
            username = f"{base_username}{counter}"
            counter += 1

        user = User(
            username=username,
            password='',           # SSO-only: no local password
            role=cfg['default_role'],
            is_default=False,
            onboarding_complete=True,  # Skip onboarding for SSO users
            oidc_sub=sub,
            oidc_provider=cfg['provider'],
            email=email or None,
            api_token=_generate_api_token(),
        )
        db.session.add(user)
        db.session.commit()
        logger.info(f"OIDC: auto-provisioned new user '{username}' with role '{cfg['default_role']}'")

    # Log in
    session.clear()
    session.permanent = True
    login_user(user, remember=True)
    session.modified = True

    logger.info(f"OIDC: user '{user.username}' logged in via {cfg['provider']}")

    response = make_response(redirect(next_url))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@oidc_bp.route('/clear_cache', methods=['POST'])
def clear_cache():
    """Admin endpoint to clear the discovery document cache (forces re-fetch)."""
    from flask_login import current_user
    if not current_user.is_authenticated or current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    _discovery_cache.clear()
    return jsonify({'ok': True, 'message': 'OIDC discovery cache cleared'})


# ---------------------------------------------------------------------------
# Settings API (used by manage_users page)
# ---------------------------------------------------------------------------

@oidc_bp.route('/settings', methods=['GET'])
def get_sso_settings():
    """Return current SSO settings (admin only)."""
    from flask_login import current_user
    from routes.settings_routes import is_user_system_enabled
    if is_user_system_enabled() and (not current_user.is_authenticated or current_user.role != 'admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    return jsonify({
        'enabled': get_setting('SSO', 'enabled', False),
        'provider': get_setting('SSO', 'provider', 'authentik'),
        'discovery_url': get_setting('SSO', 'discovery_url', ''),
        'client_id': get_setting('SSO', 'client_id', ''),
        'client_secret': get_setting('SSO', 'client_secret', ''),
        'default_role': get_setting('SSO', 'default_role', 'user'),
        'auto_provision': get_setting('SSO', 'auto_provision', True),
        'redirect_uri_base': get_setting('SSO', 'redirect_uri_base', ''),
        'disable_local_auth': get_setting('SSO', 'disable_local_auth', False),
    })


@oidc_bp.route('/settings', methods=['POST'])
def save_sso_settings():
    """Save SSO settings (admin only)."""
    from flask_login import current_user
    from routes.settings_routes import is_user_system_enabled
    from utilities.settings import load_config, save_config
    if is_user_system_enabled() and (not current_user.is_authenticated or current_user.role != 'admin'):
        return jsonify({'error': 'Unauthorized'}), 403

    body = request.get_json(silent=True) or {}
    valid_roles = ('user', 'requester', 'admin')
    valid_providers = ('authentik', 'authelia', 'generic')

    config = load_config()
    sso = config.setdefault('SSO', {})
    sso['enabled'] = bool(body.get('enabled', False))
    provider = str(body.get('provider', 'authentik'))
    sso['provider'] = provider if provider in valid_providers else 'authentik'
    sso['discovery_url'] = str(body.get('discovery_url', '')).strip()
    sso['client_id'] = str(body.get('client_id', '')).strip()
    # Only overwrite client_secret if one was actually sent (non-empty)
    new_secret = str(body.get('client_secret', ''))
    if new_secret:
        sso['client_secret'] = new_secret
    role = str(body.get('default_role', 'user'))
    sso['default_role'] = role if role in valid_roles else 'user'
    sso['auto_provision'] = bool(body.get('auto_provision', True))
    sso['redirect_uri_base'] = str(body.get('redirect_uri_base', '')).strip().rstrip('/')
    sso['disable_local_auth'] = bool(body.get('disable_local_auth', False))

    save_config(config)
    # Clear discovery cache so new URL is fetched immediately
    _discovery_cache.clear()
    logger.info(f"SSO settings updated by '{current_user.username if current_user.is_authenticated else 'system'}'")
    return jsonify({'ok': True})
