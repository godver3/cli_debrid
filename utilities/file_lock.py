import os
import sys
import time

class FileLock:
    def __init__(self, file_obj):
        self.file_obj = file_obj
        self.is_windows = sys.platform.startswith('win')
        
    def acquire(self):
        if self.is_windows:
            # Windows implementation using msvcrt
            while True:
                try:
                    import msvcrt
                    # Try to acquire lock
                    msvcrt.locking(self.file_obj.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except IOError:
                    # If file is locked, wait and retry
                    time.sleep(0.1)
        else:
            # Unix implementation using fcntl
            import fcntl
            fcntl.flock(self.file_obj, fcntl.LOCK_EX)
    
    def release(self):
        if self.is_windows:
            import msvcrt
            # Release the lock
            try:
                self.file_obj.seek(0)
                msvcrt.locking(self.file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            except IOError:
                # If for some reason we can't unlock, just pass
                pass
        else:
            import fcntl
            fcntl.flock(self.file_obj, fcntl.LOCK_UN)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release() 