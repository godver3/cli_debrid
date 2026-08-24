"""
Cloudflare managed-challenge bypass via a real, headed Chrome instance running
under a virtual display (Xvfb).

FlixPatrol (and potentially other sites) front their pages with Cloudflare's
"managed challenge" (JS proof-of-work + bot-fingerprint check). Headless
Chrome — even with stealth patches — fails this check; only a *headed*
browser (real rendering pipeline, no headless-specific fingerprint) passes it
reliably. Launching a full browser per-request is far too expensive, so this
module launches one on demand, solves the challenge once, and caches the
resulting `cf_clearance` cookie + matching User-Agent to disk. Callers reuse
that cookie with plain `requests` until it expires or a request 403s again.

Requires `patchright`, Google Chrome (or Chromium), and `xvfb` to be
installed. Deliberately optional at import time — if unavailable, callers get
a `None` cookie and should degrade gracefully.
"""

import json
import logging
import os
import time
from threading import Lock
from typing import Dict, Optional

DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
_CACHE_FILE = os.path.join(DB_CONTENT_DIR, 'cloudflare_clearance_cache.json')

# cf_clearance is typically valid for a while, but refresh well before any
# plausible expiry to avoid relying on stale clearance in production.
_MAX_COOKIE_AGE_SECONDS = 2 * 60 * 60  # 2 hours

_refresh_lock = Lock()


def _browser_environment(user_data_dir: str) -> Dict[str, str]:
    """Return an environment where Chromium can initialize its profile.

    ``gosu`` changes the process UID but preserves the container's root HOME.
    Chromium then cannot create its crashpad database under ``/root`` and
    aborts before Patchright can connect. Keep all browser-owned state inside
    the already-private, writable temporary profile instead.
    """
    browser_env = os.environ.copy()
    browser_env['HOME'] = user_data_dir
    browser_env['XDG_CONFIG_HOME'] = os.path.join(user_data_dir, '.config')
    browser_env['XDG_CACHE_HOME'] = os.path.join(user_data_dir, '.cache')
    return browser_env


def _read_page_title(page) -> Optional[str]:
    """Read a page title without failing a solve during a redirect.

    Cloudflare may replace the challenge document between ``goto`` completing
    and the title poll. Patchright reports that normal navigation race as a
    destroyed execution context. Returning ``None`` keeps the existing bounded
    polling loop alive; unrelated browser errors still propagate.
    """
    try:
        return page.title().lower()
    except Exception as exc:
        if 'execution context was destroyed' not in str(exc).lower():
            raise
        logging.debug(
            "[CloudflareBypass] Page navigated while reading its title; "
            "waiting for the new document"
        )
        return None


def _wait_for_challenge_resolution(page, timeout_seconds: int) -> bool:
    """Wait for Cloudflare's interstitial to finish within a fixed deadline."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        title = _read_page_title(page)
        if title is None:
            time.sleep(0.25)
            continue
        if 'moment' not in title and 'loading' not in title and 'checking' not in title:
            return True
        time.sleep(1)
    return False


def _lock_file_pid_alive(lock_path: str) -> bool:
    """Check whether the PID recorded in a Xvfb /tmp/.X<N>-lock file still
    refers to a live process. The lock file's existence alone isn't enough -
    Xvfb dying hard (OOM-killed, kill -9, crash) commonly leaves BOTH the
    socket and this lock file behind without either being cleaned up. If a
    process happens to be re-using that PID for something unrelated
    (theoretically possible after a PID wraparound) this returns a false
    positive and skips cleanup, which just falls back to the pre-existing
    "assume it's live" behavior rather than making anything worse.
    """
    try:
        with open(lock_path, 'r') as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        # Unreadable or malformed lock file content - can't confirm it's
        # alive, so don't touch it (same conservative default as before).
        return True
    return os.path.exists(f'/proc/{pid}')


def _clean_stale_x11_sockets():
    """Remove orphaned /tmp/.X11-unix/X<N> sockets left behind by a Xvfb
    process that died abnormally (crash, container restart, kill) without
    unlinking its own socket file. A genuinely running Xvfb always has both
    the socket AND a matching /tmp/.X<N>-lock file (containing its PID); a
    socket with no matching lock file, or with a lock file whose recorded
    PID is no longer running, is unambiguously dead. Left alone, every
    subsequent Display() attempt collides with the same stale socket and
    fails with "server already running" forever, since nothing else ever
    cleans it up.
    """
    x11_dir = '/tmp/.X11-unix'
    if not os.path.isdir(x11_dir):
        return
    try:
        for name in os.listdir(x11_dir):
            if not name.startswith('X'):
                continue
            try:
                display_nr = int(name[1:])
            except ValueError:
                continue
            lock_path = f'/tmp/.X{display_nr}-lock'
            stale_socket = os.path.join(x11_dir, name)
            if os.path.exists(lock_path):
                if _lock_file_pid_alive(lock_path):
                    continue  # lock file's PID is genuinely running - leave it alone
                try:
                    os.remove(lock_path)
                    logging.info(f"[CloudflareBypass] Removed stale X11 lock file {lock_path} (recorded PID no longer running)")
                except OSError as e:
                    logging.debug(f"[CloudflareBypass] Could not remove stale X11 lock file {lock_path}: {e}")
            try:
                os.remove(stale_socket)
                logging.info(f"[CloudflareBypass] Removed stale X11 socket {stale_socket}")
            except OSError as e:
                logging.debug(f"[CloudflareBypass] Could not remove stale X11 socket {stale_socket}: {e}")
    except Exception as e:
        logging.debug(f"[CloudflareBypass] Error scanning {x11_dir} for stale sockets: {e}")


def _load_cache() -> Optional[Dict]:
    try:
        with open(_CACHE_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict) or 'domains' not in data:
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_cache(data: Dict):
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    tmp_path = _CACHE_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f)
    os.replace(tmp_path, _CACHE_FILE)


def _solve_challenge(url: str, timeout_seconds: int = 60) -> Optional[Dict]:
    """
    Launch a real, headed Chrome (via Xvfb) to solve the Cloudflare challenge
    for `url`'s domain. Returns {'cookies': {name: value}, 'user_agent': str}
    on success, or None if the challenge could not be solved or the required
    dependencies (patchright/Chrome/Xvfb) are unavailable.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        logging.warning("[CloudflareBypass] patchright not installed — cannot solve challenge")
        return None

    import tempfile
    import shutil

    # pyvirtualdisplay wraps Xvfb lifecycle management; fall back to raw Xvfb
    # subprocess if it's not installed.
    display = None
    try:
        from pyvirtualdisplay import Display
        _clean_stale_x11_sockets()
        display = Display(visible=False, size=(1280, 800))
        display.start()
    except ImportError:
        logging.warning("[CloudflareBypass] pyvirtualdisplay not installed — cannot create virtual display")
        return None
    except Exception as e:
        logging.warning(f"[CloudflareBypass] Failed to start virtual display: {e}")
        return None

    user_data_dir = tempfile.mkdtemp(prefix='cf_bypass_')
    # Chrome/Chromium refuses to start as root without --no-sandbox — cli_debrid's
    # Docker container runs as root by default (see Dockerfile PUID/PGID handling).
    launch_args = ['--no-sandbox'] if hasattr(os, 'geteuid') and os.geteuid() == 0 else []
    # Chrome's crashpad_handler subprocess has been observed failing to
    # initialize in this container environment ("Target page, context or
    # browser has been closed", "crashpad recvmsg: Connection reset by peer").
    # The originally identified cause was a non-writable crash-database dir,
    # which _browser_environment's HOME/XDG fix now addresses - but the
    # original investigation also flagged a possible restricted-IPC-socket
    # cause that fix doesn't touch. Keep suppressing crash-reporter outright:
    # nothing in this headless Cloudflare-solving flow consumes its crash
    # reports, so there's no downside to disabling it, and doing so closes
    # off any crashpad failure mode regardless of which cause is real on a
    # given host. Playwright already passes --disable-breakpad by default,
    # but that alone doesn't stop Chromium from spawning crashpad_handler.
    launch_args.append('--disable-crash-reporter')
    browser_env = _browser_environment(user_data_dir)
    try:
        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=False,  # headed mode under Xvfb is required — headless fails the challenge
                    no_viewport=True,
                    args=launch_args,
                    env=browser_env,
                )
            except Exception as e:
                # The image installs Patchright's bundled Chromium, not the
                # system "chrome" channel. Log launch failures directly rather
                # than hiding the useful first exception behind a fallback.
                logging.warning(f"[CloudflareBypass] Failed to launch bundled Chromium: {e}")
                return None

            try:
                page = context.new_page()
                page.goto(url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")

                if not _wait_for_challenge_resolution(page, timeout_seconds):
                    logging.warning(f"[CloudflareBypass] Challenge for {url} did not resolve within {timeout_seconds}s")
                    context.close()
                    return None

                user_agent = page.evaluate("navigator.userAgent")
                cookies = {c['name']: c['value'] for c in context.cookies() if c['name'] == 'cf_clearance'}
                context.close()

                if not cookies:
                    logging.warning(f"[CloudflareBypass] No cf_clearance cookie obtained for {url}")
                    return None

                logging.info(f"[CloudflareBypass] Solved challenge for {url}")
                return {'cookies': cookies, 'user_agent': user_agent}
            except Exception as e:
                logging.warning(f"[CloudflareBypass] Error solving challenge for {url}: {e}")
                try:
                    context.close()
                except Exception:
                    pass
                return None
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        if display:
            try:
                display.stop()
            except Exception as e:
                # Swallowing this silently is exactly what let stale sockets
                # accumulate unnoticed - log it so a failed cleanup is visible.
                logging.debug(f"[CloudflareBypass] display.stop() failed (may leave a stale X11 socket): {e}")


def get_clearance(domain: str, challenge_url: str, force_refresh: bool = False) -> Optional[Dict]:
    """
    Get a valid {'cookies': {...}, 'user_agent': str} for `domain`, refreshing
    via a real browser if the cached clearance is missing, stale, or
    force_refresh is True. Thread-safe; concurrent callers block on the same
    refresh rather than launching duplicate browsers.

    Returns None if clearance could not be obtained (caller should proceed
    without it / degrade gracefully — this is best-effort, not guaranteed).
    """
    with _refresh_lock:
        cache = _load_cache() or {'domains': {}}
        entry = cache['domains'].get(domain)

        if not force_refresh and entry and (time.time() - entry.get('obtained_at', 0)) < _MAX_COOKIE_AGE_SECONDS:
            return {'cookies': entry['cookies'], 'user_agent': entry['user_agent']}

        result = _solve_challenge(challenge_url)
        if not result:
            # Keep serving stale clearance rather than nothing, if we have it —
            # it may still work even past our conservative refresh window.
            if entry:
                logging.warning(f"[CloudflareBypass] Refresh failed for {domain}, reusing stale clearance")
                return {'cookies': entry['cookies'], 'user_agent': entry['user_agent']}
            return None

        cache['domains'][domain] = {
            'cookies': result['cookies'],
            'user_agent': result['user_agent'],
            'obtained_at': time.time(),
        }
        _save_cache(cache)
        return result


def invalidate(domain: str):
    """Drop cached clearance for `domain`, forcing a fresh solve on next get_clearance() call."""
    with _refresh_lock:
        cache = _load_cache() or {'domains': {}}
        if domain in cache.get('domains', {}):
            del cache['domains'][domain]
            _save_cache(cache)
