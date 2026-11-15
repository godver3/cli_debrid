"""A short, readable progress feed for one requested title (home page command bar).

The queues tag every log line written for an item with ``[item N]``
(utilities.log_context). This reads the tail of debug.log, keeps the lines for the
requested title's items, and turns the handful that matter (state changes, scraper
result counts, releases tried, NZB/torrent outcomes) into one short line each.
Everything else (per-release rejections, request URLs, timings) is dropped.
"""
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

TAIL_BYTES = 3 * 1024 * 1024
MAX_EVENTS = 300
# Items in these states need nothing more from the queues.
DONE_STATES = {'Collected', 'Upgrading', 'Blacklisted', 'Sleeping', 'Unreleased'}

_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - \S+ - (INFO|WARNING|ERROR) - \[item (\d+)\] (.*)$')
_STATE_RE = re.compile(r'^\S.*?: ([A-Za-z_ ]+?|new) -> ([A-Za-z_ ]+?)(?: after (\S+) in [A-Za-z_ ]+)?$')
_PREFIX_RE = re.compile(r'^(\[[^\]]+\] )+')

_RULES = [
    (re.compile(r"Scraper (\S+) \(\w+\) call took [\d.]+s, found (\d+) results"),
     lambda m: f"{m[1]}: {m[2]} results", 'scrape'),
    (re.compile(r"\[NZBSeason\] '(\S+)' S\d+E\d+ served from season prefetch \((\d+) results"),
     lambda m: f"{m[1]}: {m[2]} results (season prefetch)", 'scrape'),
    (re.compile(r"\[scrape_main\] Final summary: (\d+) passed results, (\d+) filtered out"),
     lambda m: f"{m[1]} releases passed your filters ({m[2]} filtered out)", 'info'),
    (re.compile(r"Submitting NZB to \S+?: (.+)$"),
     lambda m: f"trying NZB {m[1]}", 'try'),
    (re.compile(r"NZB submitted successfully"),
     lambda m: "NZB accepted, downloading", 'ok'),
    (re.compile(r"server missing segments for '(.+?)'"),
     lambda m: f"✗ missing articles: {m[1]}", 'bad'),
    (re.compile(r"Skipping '(.+?)' — already confirmed missing segments"),
     lambda m: f"✗ skipped (already known broken): {m[1]}", 'bad'),
    (re.compile(r"\[Result \d+/\d+\] Successfully added torrent"),
     lambda m: "torrent added to debrid", 'ok'),
    (re.compile(r"\[Result \d+/\d+\] Processing: (.+)$"),
     lambda m: f"checking {m[1]}", 'try'),
    (re.compile(r"No suitable results found after processing all options"),
     lambda m: "✗ none of the releases could be added", 'bad'),
    (re.compile(r"Falling back to single scraper"),
     lambda m: "retrying with an episode-by-episode search", 'info'),
    (re.compile(r"Moved to pending uncached|moved to Pending Uncached", re.I),
     lambda m: "waiting for an uncached download", 'info'),
    (re.compile(r"ffprobe.*(?:reject|unreadable|failed)", re.I),
     lambda m: "✗ downloaded file failed the playback check", 'bad'),
    (re.compile(r"symlink(?:ed)? .*(?:created|for item)", re.I),
     lambda m: "symlink created", 'ok'),
]


def log_path() -> str:
    return os.path.join(os.environ.get('USER_LOGS', '/user/logs'), 'debug.log')


def item_label(row: Dict, multi_version: bool) -> str:
    if row.get('type') == 'episode':
        s, e = row.get('season_number'), row.get('episode_number')
        label = f"S{int(s or 0):02d}E{int(e or 0):02d}"
    else:
        label = 'movie'
    if multi_version and row.get('version'):
        label += f" {row['version']}"
    return label


# What a state change means, for the moves that need saying.
_STATE_NOTES = {
    ('Checking', 'Wanted'): "download rejected, searching again",
    ('Adding', 'Wanted'): "nothing could be added, searching again",
    (None, 'Collected'): "✓ in your library",
    (None, 'Sleeping'): "no usable release yet, will retry later",
    (None, 'Blacklisted'): "✗ gave up on it",
    (None, 'Unreleased'): "not released yet",
    (None, 'Pending Uncached'): "waiting for an uncached download",
    (None, 'Upgrading'): "✓ in your library (watching for upgrades)",
}


def simplify(message: str, detailed: bool = True) -> Optional[tuple]:
    """(text, kind) for a log message worth showing, else None."""
    m = _STATE_RE.match(message)
    if m:
        old, new = m[1], m[2]
        note = _STATE_NOTES.get((old, new)) or _STATE_NOTES.get((None, new))
        text = f"{old} → {new}" + (f" ({m[3]})" if m[3] else '') + (f": {note}" if note else '')
        return text, 'state'
    body = _PREFIX_RE.sub('', message)
    for rx, fmt, kind in _RULES:
        hit = rx.search(body)
        if hit:
            if not detailed and kind in ('scrape', 'try'):
                return None
            return fmt(hit), kind
    return None


def _tail_lines(path: str, max_bytes: int = TAIL_BYTES) -> List[str]:
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', 'replace').splitlines()
    return lines[1:] if size > max_bytes else lines   # first line may be cut


def events_for(items: List[Dict], since: str, path: Optional[str] = None) -> List[Dict]:
    """Simplified log events (oldest first) for these items, at or after ``since``."""
    labels = {}
    multi_version = len({r.get('version') for r in items}) > 1
    for row in items:
        labels[str(row['id'])] = item_label(row, multi_version)
    if not labels:
        return []
    # Many episodes: per-scraper counts and every release tried would flood the terminal.
    detailed = len(labels) <= 3
    show_label = len(labels) > 1 or items[0].get('type') == 'episode'
    out = []
    last_state = {}    # item -> (second, "Old -> New") of its last state line
    open_scrape = {}   # item -> its running "searched N sources" event
    for line in _tail_lines(path or log_path()):
        m = _LINE_RE.match(line)
        if not m or m[1] < since or m[3] not in labels:
            continue
        simple = simplify(m[4], detailed)
        if not simple:
            continue
        text, kind = simple
        item = m[3]
        if kind == 'state':
            # The queue manager and the database writer can both log one move.
            move = (m[1][:19], text.split(' (')[0].split(':')[0])
            if last_state.get(item) == move:
                continue
            last_state[item] = move
            open_scrape.pop(item, None)
        elif kind == 'scrape':
            # One line per search, not one per scraper.
            n = int(re.search(r': (\d+) results', text)[1])
            agg = open_scrape.get(item)
            if agg is None:
                agg = {'ts': m[1], 'time': m[1][11:19], 'item': labels[item] if show_label else '',
                       'kind': 'scrape', '_sources': 0, '_results': 0}
                open_scrape[item] = agg
                out.append(agg)
            agg['_sources'] += 1
            agg['_results'] += n
            agg['text'] = (f"searched {agg['_sources']} source{'s' if agg['_sources'] != 1 else ''}: "
                           f"{agg['_results']} results")
            continue
        if m[2] == 'ERROR' and kind not in ('bad', 'state'):
            kind = 'bad'
        out.append({'ts': m[1], 'time': m[1][11:19], 'item': labels[item] if show_label else '',
                    'text': text, 'kind': kind})
    for ev in out:
        ev.pop('_sources', None)
        ev.pop('_results', None)
    return out[-MAX_EVENTS:]


_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}$')


def now_ts() -> str:
    """Now as a log timestamp (local time, like the log's asctime)."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:23]


def valid_ts(value: str) -> bool:
    return bool(_TS_RE.match(value or ''))


def summarize(items: List[Dict]) -> Dict:
    counts: Dict[str, int] = {}
    for row in items:
        counts[row.get('state') or '?'] = counts.get(row.get('state') or '?', 0) + 1
    done = bool(items) and all((row.get('state') in DONE_STATES) for row in items)
    return {'counts': counts, 'done': done, 'total': len(items)}
