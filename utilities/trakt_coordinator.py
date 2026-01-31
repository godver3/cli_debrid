"""
Global Trakt API Coordinator
Provides global rate limiting and coordination for all Trakt API requests across the application
"""

import threading
import time
import logging
from datetime import datetime, timedelta

class GlobalTraktCoordinator:
    """
    Singleton class to coordinate all Trakt API requests across the application.
    Ensures global rate limiting is respected even when multiple code paths
    make concurrent API calls.
    """

    _instance = None
    _lock = threading.Lock()
    _global_cooldown_until = None

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls):
        """Get the singleton instance of GlobalTraktCoordinator"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_global_cooldown(self, seconds):
        """
        Set a global cooldown period during which all API requests should wait.
        This is typically called when receiving a 429 (Too Many Requests) response.

        Args:
            seconds: Number of seconds to wait before allowing any API requests
        """
        with self._lock:
            cooldown_until = datetime.now() + timedelta(seconds=seconds)

            # Only update if new cooldown is longer than existing
            if self._global_cooldown_until is None or cooldown_until > self._global_cooldown_until:
                self._global_cooldown_until = cooldown_until
                logging.warning(f"🚦 Trakt API global cooldown set: {seconds}s until {cooldown_until.strftime('%H:%M:%S')}")

    def wait_if_needed(self):
        """
        Check if there's a global cooldown in effect and wait if necessary.
        This should be called BEFORE making any Trakt API request.

        Returns:
            float: Number of seconds waited (0 if no wait needed)
        """
        with self._lock:
            if self._global_cooldown_until is None:
                return 0

            now = datetime.now()
            if now < self._global_cooldown_until:
                wait_seconds = (self._global_cooldown_until - now).total_seconds()
                logging.info(f"⏸️ Trakt API cooldown active, waiting {wait_seconds:.1f}s...")
                time.sleep(wait_seconds)
                return wait_seconds
            else:
                # Cooldown expired, clear it
                self._global_cooldown_until = None
                return 0

    def get_cooldown_status(self):
        """
        Get the current cooldown status.

        Returns:
            dict: Status information including active status and remaining time
        """
        with self._lock:
            if self._global_cooldown_until is None:
                return {
                    'active': False,
                    'remaining_seconds': 0,
                    'until': None
                }

            now = datetime.now()
            if now < self._global_cooldown_until:
                return {
                    'active': True,
                    'remaining_seconds': (self._global_cooldown_until - now).total_seconds(),
                    'until': self._global_cooldown_until.isoformat()
                }
            else:
                # Cooldown expired
                self._global_cooldown_until = None
                return {
                    'active': False,
                    'remaining_seconds': 0,
                    'until': None
                }

    def clear_cooldown(self):
        """Clear any active cooldown (for testing/debugging purposes)"""
        with self._lock:
            self._global_cooldown_until = None
            logging.info("🔓 Trakt API cooldown cleared")
