"""Shared app version helper — reads version.txt once, works frozen or unfrozen."""

import os
import sys

_cached_version = None


def get_app_version() -> str:
    global _cached_version
    if _cached_version is not None:
        return _cached_version

    try:
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        version_path = os.path.join(application_path, 'version.txt')
        with open(version_path, 'r') as version_file:
            _cached_version = version_file.readline().strip() or "0.0.0"
    except Exception:
        _cached_version = "0.0.0"

    return _cached_version
