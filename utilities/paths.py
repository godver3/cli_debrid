"""Where cli_debrid keeps its files: the USER_CONFIG, USER_DB_CONTENT and USER_LOGS folders.

main.py and windows_wrapper.py set these (the app data folder on Windows, /user/... in
Docker and elsewhere), but some modules are imported before they do. Call these when a
path is needed instead of building one at import time or hardcoding /user/....
"""
import os


def config_dir(*parts: str) -> str:
    return os.path.join(os.environ.get('USER_CONFIG', '/user/config'), *parts)


def db_content_dir(*parts: str) -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), *parts)


def logs_dir(*parts: str) -> str:
    return os.path.join(os.environ.get('USER_LOGS', '/user/logs'), *parts)
