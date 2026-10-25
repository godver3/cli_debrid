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


# Folders that used to be written through a hardcoded /user/... path. On Windows
# that resolved to C:\user\... instead of the app data folder these now follow.
_LEGACY_FOLDERS = (
    ('/user/config', config_dir, ('overlay_assets', 'overlay_layouts', 'overlay_templates',
                                  'badge_assets', 'poster_backups')),
    ('/user/db_content', db_content_dir, ('backups',)),
)


def migrate_legacy_user_dirs() -> None:
    """Copy those folders into the configured location, once.

    A folder is copied only when the new one is missing or empty, so nothing in
    use is merged into or overwritten; the old copy is left where it was. In
    Docker the two locations are the same and nothing happens.
    """
    import logging
    import shutil

    for legacy_root, new_dir, names in _LEGACY_FOLDERS:
        legacy_root = os.path.abspath(legacy_root)
        if os.path.normcase(os.path.abspath(new_dir())) == os.path.normcase(legacy_root):
            continue
        for name in names:
            src, dst = os.path.join(legacy_root, name), new_dir(name)
            try:
                if not os.path.isdir(src) or (os.path.isdir(dst) and os.listdir(dst)):
                    continue
                shutil.copytree(src, dst, dirs_exist_ok=True)
                logging.info(f"Copied {src} to {dst} (it was written to the old hardcoded location)")
            except Exception as e:
                logging.warning(f"Could not copy {src} to {dst}: {e}")
