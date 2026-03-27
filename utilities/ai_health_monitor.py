"""
ai_health_monitor.py — Phase 3: Proactive health checks for the AI Butler.

Runs a background daemon thread that periodically inspects app state and
sends plain-English alerts via the existing notification system when
problems are detected.

Checks performed:
  - Queue stuck (non-zero queue count hasn't changed in N minutes)
  - High blacklist rate (too many newly blacklisted items recently)
  - High error rate (many ERROR/CRITICAL lines in recent log tail)
  - Upgrading queue stalled (upgrading items present but count not changing)
  - DB size growing unusually fast

Each check type has an independent cooldown so one issue doesn't spam.
The monitor only runs when AI Assistant is enabled in settings.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# How often to run all checks (seconds)
DEFAULT_INTERVAL = 15 * 60  # 15 minutes

# Per-check cooldown (seconds) — don't re-alert on the same issue too often
COOLDOWN = {
    'stuck_queue':       2 * 60 * 60,   # 2 hours
    'high_blacklist':    4 * 60 * 60,   # 4 hours
    'high_error_rate':   2 * 60 * 60,   # 2 hours
    'stalled_upgrading': 3 * 60 * 60,   # 3 hours
    'db_size':           6 * 60 * 60,   # 6 hours
}

# Thresholds
STUCK_QUEUE_UNCHANGED_MINUTES = 30   # queue count unchanged for this long → stuck
HIGH_BLACKLIST_24H = 20              # more than this many new blacklists in 24h → alert
HIGH_ERROR_COUNT = 30                # more than this many errors in last 1000 log lines → alert
DB_SIZE_WARNING_MB = 2048            # alert if media_items.db exceeds this size


class AIHealthMonitor:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._last_alert: dict[str, float] = {}   # check_name → last alert timestamp
        self._prev_queue_snapshot: dict[str, int] = {}
        self._prev_queue_snapshot_time: float = 0.0
        self._prev_upgrading_count: int = -1
        self._prev_upgrading_time: float = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name='ai-health-monitor', daemon=True)
        self._thread.start()
        logger.info("AI Health Monitor started")

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        # Wait a bit after startup before first check so the app is fully up
        self._stop_event.wait(timeout=120)
        while not self._stop_event.is_set():
            try:
                self._run_checks()
            except Exception as e:
                logger.error(f"AI Health Monitor: unhandled error in check loop: {e}", exc_info=True)
            self._stop_event.wait(timeout=self._get_interval())

    def _get_interval(self):
        try:
            from utilities.settings import get_setting
            val = get_setting('AI Assistant', 'health_check_interval', DEFAULT_INTERVAL)
            return max(300, int(val))  # minimum 5 minutes
        except Exception:
            return DEFAULT_INTERVAL

    def _is_enabled(self):
        """Returns True if the health monitor should run checks at all."""
        try:
            from utilities.settings import get_setting
            return (bool(get_setting('AI Assistant', 'enabled', False)) and
                    bool(get_setting('AI Assistant', 'enable_proactive_notifications', True)))
        except Exception:
            return False

    def _external_notifications_enabled(self):
        """Returns True if health alerts should also be sent to external notification channels."""
        try:
            from utilities.settings import get_setting
            return bool(get_setting('AI Assistant', 'health_notifications', True))
        except Exception:
            return False

    def _cooldown_ok(self, check_name: str) -> bool:
        last = self._last_alert.get(check_name, 0)
        return (time.time() - last) >= COOLDOWN.get(check_name, 3600)

    def _mark_alerted(self, check_name: str):
        self._last_alert[check_name] = time.time()

    def _send_alert(self, message: str):
        """Log the alert and, if health_notifications is enabled, push to external channels."""
        logger.info(f"AI Health Monitor: alert — {message[:200]}")
        if not self._external_notifications_enabled():
            return
        try:
            from routes.ai_routes import send_ai_notification, _get_display_name
            send_ai_notification(message, title=f'{_get_display_name()} Health Alert')
        except Exception as e:
            logger.error(f"AI Health Monitor: failed to send external notification: {e}")

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_stuck_queue(self):
        """Alert if any processing queue hasn't changed in STUCK_QUEUE_UNCHANGED_MINUTES."""
        try:
            from queues.queue_manager import QueueManager
            qm = QueueManager()
            snapshot = {}
            for name in ['Scraping', 'Adding', 'Checking', 'Collecting']:
                q = getattr(qm, f'{name.lower()}_queue', None)
                if q is not None:
                    try:
                        snapshot[name] = q.qsize()
                    except Exception:
                        pass

            now = time.time()

            if self._prev_queue_snapshot and self._prev_queue_snapshot_time:
                elapsed_minutes = (now - self._prev_queue_snapshot_time) / 60
                if elapsed_minutes >= STUCK_QUEUE_UNCHANGED_MINUTES:
                    stuck = []
                    for name, count in snapshot.items():
                        if count > 0 and self._prev_queue_snapshot.get(name) == count:
                            stuck.append(f"{name} ({count} items)")
                    if stuck and self._cooldown_ok('stuck_queue'):
                        self._send_alert(
                            f"Queue(s) appear stuck — counts unchanged for {int(elapsed_minutes)} minutes:\n"
                            + "\n".join(f"  • {s}" for s in stuck)
                            + "\n\nConsider pausing and resuming the queue, or restarting the program."
                        )
                        self._mark_alerted('stuck_queue')
                    # Reset snapshot time so we don't keep alerting every interval
                    self._prev_queue_snapshot = snapshot
                    self._prev_queue_snapshot_time = now
            else:
                self._prev_queue_snapshot = snapshot
                self._prev_queue_snapshot_time = now

        except Exception as e:
            logger.debug(f"AI Health Monitor: stuck_queue check failed: {e}")

    def _check_high_blacklist(self):
        """Alert if too many items were blacklisted in the last 24 hours."""
        if not self._cooldown_ok('high_blacklist'):
            return
        try:
            from database import get_db_connection
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM media_items
                WHERE state = 'Blacklisted'
                AND updated_at >= datetime('now', '-1 day')
            """)
            count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM media_items WHERE state = 'Blacklisted'")
            total = c.fetchone()[0]
            conn.close()

            if count >= HIGH_BLACKLIST_24H:
                self._send_alert(
                    f"High blacklist rate detected: {count} items blacklisted in the last 24 hours "
                    f"(total blacklisted: {total}).\n\n"
                    "This may indicate a scraper source issue, bad torrent naming, or incorrect version filters. "
                    "Check Settings → Scraping and review the Blacklisted queue."
                )
                self._mark_alerted('high_blacklist')
        except Exception as e:
            logger.debug(f"AI Health Monitor: high_blacklist check failed: {e}")

    def _check_high_error_rate(self):
        """Alert if there are many errors in the recent log tail."""
        if not self._cooldown_ok('high_error_rate'):
            return
        try:
            log_dir = os.environ.get('USER_LOGS', '/user/logs')
            log_path = os.path.join(log_dir, 'debug.log')
            if not os.path.exists(log_path):
                return
            with open(log_path, 'rb') as f:
                # Read only the last ~200 KB to avoid loading the full log into memory
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 204800))
                raw = f.read()
            lines = raw.decode('utf-8', errors='replace').splitlines()[-1000:]
            errors = [l for l in lines if ' - ERROR - ' in l or ' - CRITICAL - ' in l]
            if len(errors) >= HIGH_ERROR_COUNT:
                last_error = errors[-1].strip()[:200] if errors else ''
                self._send_alert(
                    f"High error rate: {len(errors)} errors/criticals in the last 1000 log lines.\n\n"
                    f"Most recent: {last_error}\n\n"
                    "Check the Logs page for details."
                )
                self._mark_alerted('high_error_rate')
        except Exception as e:
            logger.debug(f"AI Health Monitor: high_error_rate check failed: {e}")

    def _check_stalled_upgrading(self):
        """Alert if items are stuck in Upgrading state without progress."""
        try:
            from database import get_db_connection
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM media_items WHERE state = 'Upgrading'")
            count = c.fetchone()[0]
            conn.close()

            now = time.time()

            if count > 0 and self._prev_upgrading_count == count and self._prev_upgrading_time > 0:
                elapsed_hours = (now - self._prev_upgrading_time) / 3600
                if elapsed_hours >= 2 and self._cooldown_ok('stalled_upgrading'):
                    self._send_alert(
                        f"{count} item(s) have been in the Upgrading state for over {int(elapsed_hours)} hours "
                        "without progress.\n\n"
                        "This may indicate the upgrade scraper isn't finding results, or the queue is paused. "
                        "Check the Upgrade Hub page."
                    )
                    self._mark_alerted('stalled_upgrading')
            else:
                self._prev_upgrading_count = count
                self._prev_upgrading_time = now

        except Exception as e:
            logger.debug(f"AI Health Monitor: stalled_upgrading check failed: {e}")

    def _check_db_size(self):
        """Alert if the main database file is very large."""
        if not self._cooldown_ok('db_size'):
            return
        try:
            db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            db_path = os.path.join(db_dir, 'media_items.db')
            if not os.path.exists(db_path):
                return
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            if size_mb >= DB_SIZE_WARNING_MB:
                self._send_alert(
                    f"Database size warning: media_items.db is {size_mb:.0f} MB.\n\n"
                    "Consider running a database cleanup from the Debug page to remove old data "
                    "and reclaim space."
                )
                self._mark_alerted('db_size')
        except Exception as e:
            logger.debug(f"AI Health Monitor: db_size check failed: {e}")

    def _run_checks(self):
        if not self._is_enabled():
            return
        logger.debug("AI Health Monitor: running checks")
        self._check_stuck_queue()
        self._check_high_blacklist()
        self._check_high_error_rate()
        self._check_stalled_upgrading()
        self._check_db_size()


# Singleton instance
_monitor = AIHealthMonitor()


def start_health_monitor():
    """Call once at app startup to begin background health checks."""
    if _monitor._is_enabled():
        _monitor.start()


def stop_health_monitor():
    """Call on app shutdown."""
    _monitor.stop()
