"""Trakt OAuth requests: device sign-in and token refresh.

The client secret is optional. Trakt apps are PKCE apps by default now and
are not issued one, and Trakt's OAuth schema accepts client_secret only as an
optional string, so it is left out of the body when unset rather than sent as
an empty or null value.
"""

import requests

# Trakt asks for this host on every OAuth call (authorize, device, token,
# refresh, revoke) instead of the API host.
TRAKT_OAUTH_URL = "https://auth.trakt.tv"
REQUEST_TIMEOUT = 10


def _body(client_id, client_secret, **fields):
    body = {'client_id': client_id, **fields}
    secret = str(client_secret or '').strip()
    if secret:
        body['client_secret'] = secret
    return body


def request_device_code(client_id):
    """Start device sign-in. The JSON has device_code, user_code,
    verification_url, expires_in and interval."""
    return requests.post(f"{TRAKT_OAUTH_URL}/oauth/device/code",
                         json={'client_id': client_id}, timeout=REQUEST_TIMEOUT)


def poll_device_token(device_code, client_id, client_secret=None):
    """Poll for the tokens. 200 = authorized, 400 = still pending; see the
    device token docs for the other codes."""
    return requests.post(f"{TRAKT_OAUTH_URL}/oauth/device/token",
                         json=_body(client_id, client_secret, code=device_code),
                         timeout=REQUEST_TIMEOUT)


def refresh_token(refresh_token, client_id, client_secret=None):
    """Exchange a refresh token. Trakt refresh tokens are single-use: on 200,
    store the returned refresh_token before anything refreshes again."""
    return requests.post(f"{TRAKT_OAUTH_URL}/oauth/token",
                         json=_body(client_id, client_secret,
                                    refresh_token=refresh_token,
                                    grant_type='refresh_token'),
                         timeout=REQUEST_TIMEOUT)
