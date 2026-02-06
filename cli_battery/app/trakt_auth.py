"""Trakt OAuth — single source of truth via utilities.settings."""

import json
import os
import requests
from datetime import datetime, timedelta, timezone
from .logger_config import logger

TRAKT_API_URL = "https://api.trakt.tv"
REQUEST_TIMEOUT = 10


def _get_setting(key, default=''):
    from utilities.settings import get_setting
    return get_setting('Trakt', key, default)


def _set_setting(key, value):
    from utilities.settings import set_setting
    set_setting('Trakt', key, value)


def is_authenticated() -> bool:
    """Check if Trakt OAuth tokens are present and not expired.

    Auto-refreshes if the token is within 1 hour of expiry.
    """
    access_token = _get_setting('access_token')
    refresh_token = _get_setting('refresh_token')
    expires_at_raw = _get_setting('expires_at')

    if not access_token and refresh_token:
        logger.info("Access token missing but refresh token available. Attempting refresh...")
        return refresh_access_token()

    if not access_token or not expires_at_raw:
        return False

    try:
        if isinstance(expires_at_raw, (int, float)):
            expires_at = datetime.fromtimestamp(float(expires_at_raw), tz=timezone.utc)
        elif isinstance(expires_at_raw, str):
            try:
                expires_at = datetime.fromtimestamp(float(expires_at_raw), tz=timezone.utc)
            except ValueError:
                expires_at = datetime.fromisoformat(expires_at_raw.replace('Z', '+00:00'))
        else:
            return False
    except Exception:
        logger.error(f"Could not parse expires_at: {expires_at_raw}")
        return False

    now = datetime.now(timezone.utc)
    if now >= expires_at - timedelta(hours=1):
        logger.info("Token expired or nearing expiration, refreshing...")
        return refresh_access_token()

    return True


def get_headers() -> dict:
    """Return HTTP headers for Trakt API requests."""
    return {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': _get_setting('client_id'),
        'Authorization': f"Bearer {_get_setting('access_token')}",
    }


def refresh_access_token() -> bool:
    """Refresh the Trakt OAuth access token using the refresh token."""
    refresh_token = _get_setting('refresh_token')
    client_id = _get_setting('client_id')
    client_secret = _get_setting('client_secret')

    if not refresh_token:
        logger.error("No refresh token available.")
        return False

    data = {
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret,
        'grant_type': 'refresh_token',
    }

    try:
        response = requests.post(f"{TRAKT_API_URL}/oauth/token", json=data, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"Network error refreshing Trakt token: {e}")
        return False

    if response.status_code == 200:
        token_data = response.json()
        now = datetime.now(timezone.utc)
        _set_setting('access_token', token_data['access_token'])
        _set_setting('refresh_token', token_data['refresh_token'])
        _set_setting('expires_at', int((now + timedelta(seconds=token_data['expires_in'])).timestamp()))
        _set_setting('last_refresh', now.isoformat())

        # Also update .pytrakt.json for backward compatibility
        _sync_to_pytrakt(token_data['access_token'], token_data['refresh_token'],
                         int((now + timedelta(seconds=token_data['expires_in'])).timestamp()),
                         now.isoformat())
        logger.info("Trakt token refreshed successfully.")
        return True

    logger.error(f"Failed to refresh Trakt token: {response.status_code} {response.text}")
    try:
        error_data = response.json()
        desc = error_data.get('error_description', '').lower()
        if 'refresh_token' in desc and any(w in desc for w in ('invalid', 'expired', 'revoked')):
            logger.error("Refresh token is invalid/expired. Manual re-auth required.")
            _set_setting('access_token', '')
            _set_setting('refresh_token', '')
            _set_setting('expires_at', '')
    except Exception:
        pass
    return False


def receive_auth(auth_data: dict) -> dict:
    """Store incoming Trakt OAuth credentials (pushed from main app auth flow).

    Returns dict with 'status' and 'message'.
    """
    try:
        _set_setting('client_id', auth_data.get('CLIENT_ID', ''))
        _set_setting('client_secret', auth_data.get('CLIENT_SECRET', ''))
        _set_setting('access_token', auth_data.get('OAUTH_TOKEN', ''))
        _set_setting('refresh_token', auth_data.get('OAUTH_REFRESH', ''))
        _set_setting('expires_at', auth_data.get('OAUTH_EXPIRES_AT', ''))
        _set_setting('last_refresh', datetime.now(timezone.utc).isoformat())

        _sync_to_pytrakt(
            auth_data.get('OAUTH_TOKEN', ''),
            auth_data.get('OAUTH_REFRESH', ''),
            auth_data.get('OAUTH_EXPIRES_AT', ''),
            datetime.now(timezone.utc).isoformat(),
        )
        logger.info("Trakt auth received and saved successfully.")
        return {'status': 'success', 'message': 'Trakt auth received and saved successfully'}
    except Exception as e:
        logger.error(f"Error receiving Trakt auth: {e}")
        return {'status': 'error', 'message': str(e)}


def check_auth() -> dict:
    """Check current Trakt auth status.

    Returns dict with 'status': 'authorized' or 'unauthorized'.
    """
    try:
        if is_authenticated():
            return {'status': 'authorized'}
        return {'status': 'unauthorized'}
    except Exception as e:
        logger.error(f"Error checking Trakt auth: {e}")
        return {'status': 'unauthorized'}


def _sync_to_pytrakt(access_token, refresh_token, expires_at, last_refresh):
    """Write credentials to .pytrakt.json for backward compat."""
    config_dir = os.environ.get('USER_CONFIG', '/user/config')
    pytrakt_path = os.path.join(config_dir, '.pytrakt.json')
    try:
        credentials = {
            'CLIENT_ID': _get_setting('client_id'),
            'CLIENT_SECRET': _get_setting('client_secret'),
            'OAUTH_TOKEN': access_token,
            'OAUTH_REFRESH': refresh_token,
            'OAUTH_EXPIRES_AT': expires_at,
            'LAST_REFRESH': last_refresh,
        }
        os.makedirs(os.path.dirname(pytrakt_path), exist_ok=True)
        with open(pytrakt_path, 'w') as f:
            json.dump(credentials, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not sync to .pytrakt.json: {e}")
