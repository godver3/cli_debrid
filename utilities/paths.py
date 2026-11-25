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


def safe_realpath(path: str) -> str:
    """os.path.realpath that works on the built-in mount on Windows.

    There, realpath raises WinError 1005 for every path on the WinFsp drive
    (GetFinalPathNameByHandle needs a volume the Mount Manager knows), so
    links are followed by hand: readlink until a non-link, minus the \\\\?\\
    prefix Windows adds. Elsewhere this is plain os.path.realpath."""
    try:
        return os.path.realpath(path)
    except OSError:
        pass
    p = os.path.abspath(path)
    for _ in range(40):  # link loop guard, like the OS limit
        try:
            if not os.path.islink(p):
                break
            target = os.readlink(p)
        except OSError:
            break
        if target.startswith('\\\\?\\UNC\\'):
            target = '\\\\' + target[8:]
        elif target.startswith('\\\\?\\'):
            target = target[4:]
        p = os.path.normpath(os.path.join(os.path.dirname(p), target))
    return os.path.normpath(p)


_MEDIA_EXTS = ('.mkv', '.mp4', '.m4v', '.avi', '.mov', '.mpg', '.mpeg', '.ts', '.m2ts', '.wmv', '.webm')


def media_file_at(path: str):
    """The file a Plex check candidate points at, or None. While an NZB item
    is still checking, its file name can still be the release name, so a
    candidate may be the job's folder: use the largest video inside it.
    A folder must never pass as the file, because a directory's st_size
    (always 0 on Windows) made the ffprobe gate reject healthy releases."""
    if os.path.isfile(path):
        return path
    if not os.path.isdir(path):
        return None
    best, best_size = None, -1
    try:
        for root, _dirs, files in os.walk(path):
            if root[len(path):].count(os.sep) > 1:
                continue
            for name in files:
                if not name.lower().endswith(_MEDIA_EXTS) or 'sample' in name.lower():
                    continue
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size > best_size:
                    best, best_size = full, size
    except OSError:
        return None
    return best
