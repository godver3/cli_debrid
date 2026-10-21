"""Cleanup helpers for Trakt OAuth state stored in two locations."""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple


_CONFIG_TOKEN_KEYS = ('access_token', 'refresh_token', 'expires_at', 'last_refresh')
_LEGACY_AUTH_KEYS = (
    'CLIENT_ID',
    'CLIENT_SECRET',
    'OAUTH_TOKEN',
    'OAUTH_REFRESH',
    'OAUTH_EXPIRES_AT',
    'LAST_REFRESH',
)


def _has_value(value: Any) -> bool:
    return bool(str(value or '').strip())


def trakt_is_configured(config: Dict[str, Any]) -> bool:
    """Return whether both credentials required for Trakt OAuth are present."""
    trakt_config = config.get('Trakt', {})
    if not isinstance(trakt_config, dict):
        return False
    return (
        _has_value(trakt_config.get('client_id'))
        and _has_value(trakt_config.get('client_secret'))
    )


def _legacy_config_path(config_dir: Optional[str] = None) -> str:
    config_dir = config_dir or os.environ.get('USER_CONFIG', '/user/config')
    return os.path.join(config_dir, '.pytrakt.json')


def reset_trakt_auth(config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Forget the Trakt authorization so it can be done again from scratch.

    What people did by hand when Trakt auth got stuck: drop the OAuth tokens
    from config.json and from the legacy .pytrakt.json. The client ID/secret
    stay, so re-authorizing is one step. The old .pytrakt.json is kept as
    .pytrakt.json.bak; a fresh one with blank tokens replaces it (that works
    even when the old file is corrupt), and the trakt library's in-memory
    copy is cleared too, so no restart is needed.
    """
    from utilities.settings import get_setting, set_setting

    cleared_config = []
    for key in _CONFIG_TOKEN_KEYS:
        if _has_value(get_setting('Trakt', key, '')):
            set_setting('Trakt', key, '')
            cleared_config.append(key)

    legacy_path = _legacy_config_path(config_dir)
    backup_path = None
    if os.path.exists(legacy_path):
        backup_path = legacy_path + '.bak'
        try:
            os.replace(legacy_path, backup_path)
        except OSError as exc:
            logging.warning(f"Could not back up Trakt auth file {legacy_path}: {exc}")
            backup_path = None
    fresh = {key: '' for key in _LEGACY_AUTH_KEYS}
    fresh['CLIENT_ID'] = get_setting('Trakt', 'client_id', '') or ''
    fresh['CLIENT_SECRET'] = get_setting('Trakt', 'client_secret', '') or ''
    temporary_path = legacy_path + '.tmp'
    try:
        os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
        with open(temporary_path, 'w') as legacy_file:
            json.dump(fresh, legacy_file, indent=2)
        os.replace(temporary_path, legacy_path)
        legacy_written = True
    except OSError as exc:
        logging.error(f"Could not write a fresh Trakt auth file {legacy_path}: {exc}")
        legacy_written = False

    # pytrakt keeps what it loaded in module globals, and its load_config()
    # only rereads the file while CLIENT_ID or CLIENT_SECRET is unset, so the
    # old token stayed in use until a restart. Unset them all: the next call
    # rereads the fresh file.
    try:
        import sys
        core = sys.modules.get('trakt.core')
        if core is not None:
            for name in ('CLIENT_ID', 'CLIENT_SECRET', 'OAUTH_TOKEN', 'OAUTH_REFRESH',
                         'OAUTH_EXPIRES_AT', 'OAUTH_TOKEN_VALID'):
                if hasattr(core, name):
                    setattr(core, name, None)
    except Exception as exc:
        logging.debug(f"Could not clear the trakt library's cached token: {exc}")

    logging.info(f"Trakt authorization reset: cleared {cleared_config or 'no'} config token(s); "
                 f"{legacy_path} {'replaced' if legacy_written else 'NOT replaced'}"
                 f"{f' (old file kept as {backup_path})' if backup_path else ''}")
    return {'config_keys_cleared': cleared_config, 'legacy_backup': backup_path,
            'legacy_written': legacy_written}


def clear_stale_trakt_auth(
    config: Dict[str, Any],
    config_dir: Optional[str] = None,
) -> Tuple[bool, bool]:
    """Clear OAuth state when the resulting Trakt credentials are incomplete.

    The supplied config is mutated in place. The legacy file is updated
    atomically. Returns ``(config_changed, legacy_changed)`` so callers can
    avoid an unnecessary main-config write.
    """
    if trakt_is_configured(config):
        return False, False

    config_changed = False
    trakt_config = config.get('Trakt')
    if trakt_config is None:
        trakt_config = {}
    elif not isinstance(trakt_config, dict):
        trakt_config = {}
        config['Trakt'] = trakt_config
        config_changed = True

    for key in _CONFIG_TOKEN_KEYS:
        if key in trakt_config and trakt_config.get(key) != '':
            config_changed = True
            trakt_config[key] = ''

    legacy_path = _legacy_config_path(config_dir)
    try:
        with open(legacy_path, 'r') as legacy_file:
            legacy_config = json.load(legacy_file)
    except FileNotFoundError:
        return config_changed, False
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning(f"Could not read stale Trakt auth file {legacy_path}: {exc}")
        return config_changed, False

    if not isinstance(legacy_config, dict):
        logging.warning(f"Could not clean stale Trakt auth file {legacy_path}: expected a JSON object")
        return config_changed, False

    legacy_changed = False
    for key in _LEGACY_AUTH_KEYS:
        if key in legacy_config and legacy_config.get(key) != '':
            legacy_changed = True
            legacy_config[key] = ''

    if legacy_changed:
        temporary_path = legacy_path + '.tmp'
        try:
            with open(temporary_path, 'w') as legacy_file:
                json.dump(legacy_config, legacy_file, indent=2)
            os.replace(temporary_path, legacy_path)
        except OSError as exc:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            logging.warning(f"Could not clear stale Trakt auth file {legacy_path}: {exc}")
            legacy_changed = False

    return config_changed, legacy_changed
