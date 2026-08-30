"""
Central secret redaction for log output.

Log files get pasted into issues, gists and support threads routinely, so
nothing that can authenticate as the user may reach them. Redaction happens at
the formatter, which is the one place every handler funnels through -- that
covers the message, its args, and any exception or stack text, no matter which
call site produced it. Individual call sites redacting by hand is what let the
whole config dict get logged verbatim in the first place.

Two independent strategies run on every record:

1. Value-based -- the actual secret values are read straight out of config.json
   and replaced wherever they appear. This is the reliable half: it catches a
   token embedded in a URL, a header, a dict repr or a traceback, without
   needing to recognise the surrounding syntax.

2. Pattern-based -- key/value pairs whose *key* looks sensitive get their value
   replaced. This covers secrets that are not in config.json: values being set,
   third-party credentials, keys read from the environment.

Nothing in this module may log. It runs inside the logging pipeline, so a log
call here would recurse.
"""

import json
import logging
import os
import re
import threading
import time

# Substrings in a key name that mark its value as sensitive. Kept specific
# enough to avoid eating useful debug output -- 'url' is deliberately absent,
# since knowing which URL was called matters and the credential is the token
# alongside it, which the value-based pass catches anyway.
_SENSITIVE_FRAGMENTS = (
    'token', 'secret', 'password', 'passwd', 'api_key', 'apikey', 'api-key',
    'client_secret', 'credential', 'private_key', 'authorization',
    'access_token', 'refresh_token', 'bearer',
    # A tracker passkey and a Discord/Slack webhook URL are each a full
    # credential on their own -- anyone holding one can act as the user.
    'passkey', 'webhook',
)

# Exact key names that are sensitive on their own. 'username' is here even
# though it isn't a credential by itself: content sources like "Other Plex
# Watchlist" store a friend's real Plex username, and log lines routinely
# narrate it in plain prose (e.g. "Starting watchlist retrieval for other
# Plex user: <username>") rather than as a key/value pair the pattern-based
# pass would catch -- only the value-based pass, seeded from this key, finds
# and replaces that literal string wherever it appears.
_SENSITIVE_EXACT = frozenset({'key', 'auth', 'pass', 'apikey', 'api_key', 'username'})

# Values shorter than this are too likely to be ordinary words to blanket
# replace across every log line.
MIN_SECRET_LEN = 6

# How often the secret list is re-read, so rotating a key starts being redacted
# without a restart.
REFRESH_INTERVAL_SECONDS = 60.0

PLACEHOLDER = '***REDACTED***'

_state = {'pattern': None, 'loaded_at': 0.0}
_lock = threading.Lock()
_guard = threading.local()


def _is_sensitive_key(key_name: str) -> bool:
    kl = str(key_name).lower()
    if kl in _SENSITIVE_EXACT:
        return True
    return any(fragment in kl for fragment in _SENSITIVE_FRAGMENTS)


# --- Strategy 1: replace known secret values wherever they appear ------------

def _config_path() -> str:
    return os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'config.json')


def _collect_secret_values(obj, out: set, depth: int = 0) -> None:
    """Walk the config and collect every value stored under a sensitive key."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                _collect_secret_values(value, out, depth + 1)
            elif _is_sensitive_key(key) and isinstance(value, str):
                candidate = value.strip()
                if len(candidate) >= MIN_SECRET_LEN:
                    out.add(candidate)
    elif isinstance(obj, list):
        for entry in obj:
            _collect_secret_values(entry, out, depth + 1)


def _load_secret_values() -> set:
    """Read config.json directly.

    Deliberately not via utilities.settings: that module logs on load, and a
    log call from inside the logging pipeline recurses.
    """
    secrets: set = set()
    try:
        path = _config_path()
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                _collect_secret_values(json.load(f), secrets)
    except Exception:
        # Never raise from the logging path -- an unredacted line beats losing
        # logging altogether, and the pattern pass still applies.
        pass

    # Environment-supplied credentials never appear in config.json.
    try:
        for env_key, env_value in os.environ.items():
            if _is_sensitive_key(env_key) and isinstance(env_value, str):
                candidate = env_value.strip()
                if len(candidate) >= MIN_SECRET_LEN:
                    secrets.add(candidate)
    except Exception:
        pass

    return secrets


def _build_value_pattern(secrets: set):
    if not secrets:
        return None
    # Longest first so a secret containing another is replaced whole.
    ordered = sorted(secrets, key=len, reverse=True)
    try:
        return re.compile('|'.join(re.escape(s) for s in ordered))
    except Exception:
        return None


def _get_value_pattern():
    now = time.time()
    if _state['pattern'] is not None and (now - _state['loaded_at']) < REFRESH_INTERVAL_SECONDS:
        return _state['pattern']

    # Only one thread reloads; the rest use whatever is currently cached.
    if not _lock.acquire(blocking=False):
        return _state['pattern']
    try:
        now = time.time()
        if _state['pattern'] is None or (now - _state['loaded_at']) >= REFRESH_INTERVAL_SECONDS:
            _state['pattern'] = _build_value_pattern(_load_secret_values())
            _state['loaded_at'] = now
        return _state['pattern']
    finally:
        _lock.release()


def refresh_secrets() -> None:
    """Force the secret list to be re-read on the next log record.

    Call after settings are saved so a newly entered key is redacted at once
    rather than up to REFRESH_INTERVAL_SECONDS later.
    """
    with _lock:
        _state['pattern'] = None
        _state['loaded_at'] = 0.0


# --- Strategy 2: replace values whose key looks sensitive -------------------

_KEY_RE = r'[A-Za-z0-9_\-]*(?:' + '|'.join(_SENSITIVE_FRAGMENTS) + r')[A-Za-z0-9_\-]*'

# 'api_key': 'value'  /  "token": "value"  -- dict reprs and JSON
_QUOTED_KV_RE = re.compile(
    r'(?P<prefix>(?P<q>["\'])' + _KEY_RE + r'(?P=q)\s*:\s*)'
    r'(?P<vq>["\'])(?P<val>(?:\\.|(?!(?P=vq)).){' + str(MIN_SECRET_LEN) + r',})(?P=vq)',
    re.IGNORECASE,
)

# api_key=value  /  token = value  -- query strings, kwargs, env dumps
_BARE_KV_RE = re.compile(
    r'(?P<prefix>\b' + _KEY_RE + r'\s*=\s*)'
    r'(?P<val>[^\s&;,\'")\]}]{' + str(MIN_SECRET_LEN) + r',})',
    re.IGNORECASE,
)

# Authorization: Bearer <token>
_BEARER_RE = re.compile(r'(?P<prefix>\bBearer\s+)(?P<val>[A-Za-z0-9._\-]{' + str(MIN_SECRET_LEN) + r',})',
                        re.IGNORECASE)

# Cheap gate: only run the three substitutions above when the text contains
# something that could plausibly match. Most log lines do not.
_MARKER_RE = re.compile('|'.join(_SENSITIVE_FRAGMENTS) + r'|\bkey\b|\bauth\b', re.IGNORECASE)


def _replace_kv(match) -> str:
    return match.group('prefix') + PLACEHOLDER


def _replace_quoted_kv(match) -> str:
    return match.group('prefix') + match.group('vq') + PLACEHOLDER + match.group('vq')


def scrub(text: str) -> str:
    """Remove credentials from a formatted log line."""
    if not text:
        return text

    # Guard against recursion: _load_secret_values touches the filesystem, and
    # anything unexpected there must not re-enter this function.
    if getattr(_guard, 'active', False):
        return text
    _guard.active = True
    try:
        pattern = _get_value_pattern()
        if pattern is not None:
            text = pattern.sub(PLACEHOLDER, text)

        if _MARKER_RE.search(text):
            text = _QUOTED_KV_RE.sub(_replace_quoted_kv, text)
            text = _BARE_KV_RE.sub(_replace_kv, text)
            text = _BEARER_RE.sub(_replace_kv, text)

        return text
    except Exception:
        return text
    finally:
        _guard.active = False


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs credentials from the fully rendered line.

    Applied at the formatter rather than as a filter so that exception and
    stack text -- which a filter never sees -- is covered too.
    """

    def format(self, record):
        return scrub(super().format(record))
