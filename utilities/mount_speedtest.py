"""
Manual speed tests for the built-in mount's providers (Mount page buttons).

Usenet: the engine downloads real articles of recent jobs from that one
server (POST /speedtest). Debrid: a ranged download of the largest file of
a recent torrent on the mount, through the same link the mount streams
with. Nothing is cached or kept; a test costs at most ~64 MB (usenet) or
~32 MB (debrid) of plan traffic.
"""

import logging
import time
from typing import Callable, Optional

import requests

DEBRID_BYTES = 32 << 20
DEBRID_SECONDS = 15
_TRIES = 5  # recent torrents tried until one gives a streamable file


def usenet(client, provider: str) -> dict:
    code, body = client._call('POST', '/speedtest', timeout=45, json={'provider': provider})
    if code == 200 and isinstance(body, dict):
        return {'ok': not body.get('error'), **body}
    err = body.get('error') if isinstance(body, dict) else None
    return {'ok': False, 'provider': provider, 'error': err or f'engine said {code}'}


def debrid(client, key: str, providers: Callable, provider_key: Callable, link: Callable,
           get: Optional[Callable] = None) -> dict:
    """key: provider key of the mount refs (rd, tb, ad, ...)."""
    get = get or requests.get
    prov = next((p for p in providers() if provider_key(p) == key), None)
    if prov is None:
        return {'ok': False, 'provider': key, 'error': f'no configured debrid provider for {key!r}'}
    jobs = [j for j in client._debrid_jobs() if str(j.get('id', '')).startswith(key + '-')]
    if not jobs:
        return {'ok': False, 'provider': key, 'error': 'no torrents on the mount to test with yet'}
    jobs.sort(key=lambda j: j.get('created_at') or '', reverse=True)
    last = 'no streamable file found'
    for j in jobs[:_TRIES]:
        torrent_id = j['id'].partition('-')[2]
        try:
            got = prov.mount_files(torrent_id)
        except Exception as exc:
            last = f'file list failed: {exc}'
            continue
        if not got:
            continue
        files = sorted(got[1], key=lambda f: int(f.get('size') or 0), reverse=True)
        if not files:
            continue
        ref = f'{key}:{torrent_id}:{files[0]["file_id"]}'
        try:
            api_start = time.monotonic()
            url, _ = link(ref)
            api_ms = (time.monotonic() - api_start) * 1000
        except Exception as exc:
            last = f'no link: {exc}'
            continue
        out = _ranged_read(get, url)
        out.update(provider=key, link_ms=round(api_ms, 1), file=files[0].get('name', ''))
        return out
    return {'ok': False, 'provider': key, 'error': last}


def _ranged_read(get: Callable, url: str) -> dict:
    start = time.monotonic()
    got, first = 0, None
    try:
        with get(url, headers={'Range': f'bytes=0-{DEBRID_BYTES - 1}'}, stream=True, timeout=10) as r:
            if r.status_code not in (200, 206):
                return {'ok': False, 'error': f'download said HTTP {r.status_code}'}
            for chunk in r.iter_content(1 << 16):
                if first is None:
                    first = time.monotonic() - start
                got += len(chunk)
                if got >= DEBRID_BYTES or time.monotonic() - start > DEBRID_SECONDS:
                    break
    except Exception as exc:
        logging.debug(f'[mount speedtest] debrid read failed: {exc}')
        if not got:
            return {'ok': False, 'error': f'download failed: {exc}'}
    secs = max(time.monotonic() - start, 1e-6)
    return {'ok': got > 0, 'bytes': got, 'seconds': round(secs, 2),
            'mb_per_second': round(got / (1 << 20) / secs, 1),
            'latency_ms': round((first or 0) * 1000, 1)}
