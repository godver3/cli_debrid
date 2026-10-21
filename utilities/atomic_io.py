import logging
import os
import shutil
import threading


def atomic_write_bytes(path, data):
    """Write ``data`` to ``path`` atomically (temp file + fsync + rename).

    Readers never observe a truncated file. Falls back to an in-place write
    when the rename is impossible, e.g. when ``path`` is a bind-mounted file.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, 'wb') as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp_path)
            except OSError:
                pass
        try:
            os.replace(tmp_path, path)
        except OSError as e_replace:
            logging.warning(f"atomic_write_bytes: rename onto {path} failed ({e_replace}); writing in place.")
            with open(path, 'wb') as f:
                f.write(data)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
