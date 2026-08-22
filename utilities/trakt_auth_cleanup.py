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
