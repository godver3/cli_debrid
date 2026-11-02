"""Simulated wall clock for the Plex bench.

The Checking queue waits minutes to hours for Plex, so the bench runs the app
against a clock it moves itself. ``time.time``/``time.sleep`` and
``datetime.now``/``utcnow``/``today`` all read it. Monotonic time is left
alone, so socket timeouts and thread waits behave normally.
"""

import datetime as _dt
import sys
import threading
import time as _time

_REAL_DATETIME = _dt.datetime
_REAL_DATE = _dt.date


class SimClock:
    def __init__(self, start=None):
        # Start at the real time so rows the database stamps itself
        # (CURRENT_TIMESTAMP) are close to what the app sees.
        self._now = float(int(start if start is not None else _time.time()))
        self._lock = threading.Lock()
        self.sleeps = 0

    def time(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += max(0.0, float(seconds))
            return self._now

    def set(self, value):
        with self._lock:
            self._now = max(self._now, float(value))

    def sleep(self, seconds):
        # The app sleeps between retries; in the bench that's instant. Time only
        # moves when the bench scheduler moves it.
        self.sleeps += 1

    def install(self, monkeypatch):
        """Point the time and datetime functions at this clock for one test."""
        monkeypatch.setattr(sys.modules[__name__], '_active', self)

        # sqlite3 only adapts the exact datetime/date types.
        import sqlite3
        sqlite3.register_adapter(SimDatetime, lambda v: v.isoformat(' '))
        sqlite3.register_adapter(SimDate, lambda v: v.isoformat())

        monkeypatch.setattr(_time, 'time', self.time)
        monkeypatch.setattr(_time, 'sleep', self.sleep)
        monkeypatch.setattr(_dt, 'datetime', SimDatetime)
        monkeypatch.setattr(_dt, 'date', SimDate)
        # Modules that did `from datetime import datetime` hold the real class.
        # Swap it in the app's own modules; libraries keep theirs.
        for module in list(sys.modules.values()):
            if module is None or not _is_app_module(module):
                continue
            if getattr(module, 'datetime', None) is _REAL_DATETIME:
                monkeypatch.setattr(module, 'datetime', SimDatetime)
            if getattr(module, 'date', None) is _REAL_DATE:
                monkeypatch.setattr(module, 'date', SimDate)
        return self


# Module level so pickle can find them (the poster cache pickles datetimes).
_active = None


class SimDatetime(_REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(_active.time() if _active else _time.time(), tz)

    @classmethod
    def utcnow(cls):
        return cls.now(_dt.timezone.utc).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()


class SimDate(_REAL_DATE):
    @classmethod
    def today(cls):
        return SimDatetime.now().date()


def _is_app_module(module):
    path = getattr(module, '__file__', None) or ''
    from tests.plex_bench import REPO_ROOT
    return path.startswith(REPO_ROOT) and '/tests/' not in path and 'site-packages' not in path
