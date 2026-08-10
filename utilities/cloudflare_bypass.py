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


def _solve_challenge(url: str, timeout_seconds: int = 30) -> Optional[Dict]:
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
    # Chrome's crashpad_handler subprocess fails to initialize in this container
    # environment (no writable crash-database dir / restricted IPC socket),
    # which kills the whole browser context right after launch ("Target page,
    # context or browser has been closed", crashpad recvmsg: Connection reset
    # by peer). Playwright already passes --disable-breakpad by default, but
    # that alone doesn't stop Chromium from spawning crashpad_handler —
    # --disable-crash-reporter is the flag that actually suppresses it.
    launch_args.append('--disable-crash-reporter')
    try:
        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=False,  # headed mode under Xvfb is required — headless fails the challenge
                    channel="chrome",
                    no_viewport=True,
                    args=launch_args,
                )
            except Exception:
                # Real Chrome channel not installed — fall back to bundled Chromium.
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    headless=False,
                    no_viewport=True,
                    args=launch_args,
                )

            try:
                page = context.new_page()
                page.goto(url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")

                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    title = page.title().lower()
                    if 'moment' not in title and 'loading' not in title and 'checking' not in title:
                        break
                    time.sleep(1)
                else:
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
            except Exception:
                pass


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
