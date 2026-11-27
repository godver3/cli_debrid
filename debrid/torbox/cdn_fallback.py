"""Is a TorBox CDN server taking connections?

TorBox hands out links on the CDN server nearest to the requester. When
that server is down (nexus-409.ecan.tb-cdn.io refused every connection
for hours on 2026-11-26 while other regions served the same files), the
link minting asks again as if from another region (requestdl's user_ip).
A server's state is remembered for a few minutes, so a normal mint costs
one quick TCP connect at most.
"""
import socket
import threading
import time
from urllib.parse import urlparse

#: Regions tried in order, with an address TorBox maps to each.
REGION_IPS = (('wnam', '8.8.8.8'), ('weur', '1.1.1.1'), ('nord', '95.216.0.1'))

_TTL_UP = 300
_TTL_DOWN = 120
_TIMEOUT = 3
_lock = threading.Lock()
_state = {}  # host -> (reachable, checked_at)


def host(url: str) -> str:
    return urlparse(url).hostname or ''


def mark_down(url: str) -> None:
    h = host(url)
    if h:
        with _lock:
            _state[h] = (False, time.time())


def reachable(url: str) -> bool:
    h = host(url)
    if not h:
        return True
    now = time.time()
    with _lock:
        got = _state.get(h)
    if got and now - got[1] < (_TTL_UP if got[0] else _TTL_DOWN):
        return got[0]
    port = urlparse(url).port or (443 if url.startswith('https') else 80)
    try:
        socket.create_connection((h, port), timeout=_TIMEOUT).close()
        ok = True
    except OSError as exc:
        # A refusal or a dead route is the server; a DNS failure is more
        # likely ours, and switching regions wouldn't help.
        ok = isinstance(exc, socket.gaierror)
    with _lock:
        # A reader can report a refusal while the TCP probe is in flight.
        current = _state.get(h)
        if current is not got and current is not None:
            return current[0]
        _state[h] = (ok, now)
    return ok
