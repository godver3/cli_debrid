"""A simulated Plex Media Server for the Plex-mode bench.

It's a real HTTP server on 127.0.0.1, so cli_debrid's own plexapi and aiohttp
code talks to it exactly as it talks to Plex. Behind it is a small model of what
matters in Plex mode:

* libraries (sections) whose locations are folders in the bench's fake mount;
* a scanner that works through refresh requests one at a time, in simulated
  time, and indexes the video files it finds (Plex queues scans, it doesn't run
  them in parallel);
* a "metadata agent": the bench registers what each file is (movie or episode,
  with its IMDb/TMDB/TVDB ids), and the scanner matches files against that.
  Files it doesn't know are added unmatched (local:// guid), like Plex does.

Knobs on ``FakePlex.behavior`` make it slow, stuck, flaky or unreachable, so the
bench can reproduce what users report. Everything it is asked is logged in
``requests`` with the simulated time, for the bench report.

Checked against a real Plex Media Server (1.41/1.42, new Plex agents) on
2026-11-01; README.md lists what was compared. In short: scans run one at a
time and a request identical to one still queued is merged; ``?file=`` is a
case-insensitive substring of the part path; ``guid=`` only matches the item's
own plex:// guid, never imdb:// or tmdb://; a show library's ``?file=`` without
``type=4`` is an HTTP 500; section recently-added lists episodes, not seasons.
"""

import json
import os
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import quoteattr

VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.wmv')


@dataclass
class Meta:
    """What the metadata agent knows about one file."""
    type: str                      # 'movie' or 'episode'
    title: str                     # movie title, or the show title for episodes
    year: Optional[int] = None
    imdb_id: Optional[str] = None  # the movie's, or the show's for episodes
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    resolution: str = '1080'


@dataclass
class Item:
    rating_key: int
    type: str                      # movie | show | season | episode
    section_key: str
    title: str
    year: Optional[int] = None
    guid: str = ''
    guids: List[str] = field(default_factory=list)
    index: Optional[int] = None
    parent_key: Optional[int] = None
    grandparent_key: Optional[int] = None
    added_at: int = 0
    updated_at: int = 0
    files: List[tuple] = field(default_factory=list)  # (path, size)
    resolution: str = '1080'


@dataclass
class ScanJob:
    section_key: str
    path: Optional[str]            # None = whole section
    requested_at: float
    started_at: Optional[float] = None


@dataclass
class Fault:
    """Make matching requests fail. ``kind``: 'error' (HTTP 500), 'drop' (close the
    connection without answering; what a client sees when Plex times out),
    'unauthorized' (401). ``times`` limits how many requests it hits."""
    pattern: str
    kind: str = 'drop'
    times: Optional[int] = None
    until: Optional[float] = None  # simulated time it stops

    def matches(self, path, now):
        if self.times is not None and self.times <= 0:
            return False
        if self.until is not None and now >= self.until:
            return False
        return re.search(self.pattern, path) is not None


@dataclass
class Behavior:
    # Scanner
    scan_overhead_s: float = 5.0         # every refresh, however small
    scan_per_file_s: float = 1.0         # per video file under the scanned path
    full_scan_s: float = 120.0           # a refresh with no path
    scanner_stuck: bool = False          # takes requests, finishes none
    coalesce_queued_scans: bool = True   # merge a request identical to one still queued (real Plex does)
    max_queue: Optional[int] = None      # requests beyond this are silently dropped
    unmatched_files: str = 'local'       # 'local': add with a local:// guid; 'skip': don't add
    never_index: set = field(default_factory=set)  # basenames the scanner skips (bad files)
    index_delay_s: float = 0.0           # extra time between a scan finishing and items appearing
    # Library listing
    group_seasons: bool = False          # list a season instead of its new episodes (real section listings don't)
    # Requests
    latency_s: Dict[str, float] = field(default_factory=dict)  # path regex -> simulated seconds per request
    faults: List[Fault] = field(default_factory=list)
    down: bool = False                   # every request is dropped


class FakePlex:
    def __init__(self, clock, token='bench-token', sections=None):
        self.clock = clock
        self.token = token
        self.behavior = Behavior()
        self.machine_id = 'fakeplex-' + uuid.uuid4().hex[:12]
        self.sections: Dict[str, dict] = {}
        self.items: Dict[int, Item] = {}
        self.catalog: Dict[str, Meta] = {}      # basename -> Meta
        self.scan_queue: deque = deque()
        self.current_scan: Optional[ScanJob] = None
        self.scan_done_at: Optional[float] = None
        self.requests: List[dict] = []
        self.scans_requested: List[dict] = []
        self.scans_completed: List[dict] = []
        self.unhandled: List[str] = []
        self.max_queue_seen = 0
        self._next_key = 1000
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        for section in sections or []:
            self.add_section(**section)

    # --- setup -------------------------------------------------------------

    def add_section(self, key, title, type, locations):
        self.sections[str(key)] = {'key': str(key), 'title': title, 'type': type,
                                   'locations': [os.path.normpath(p) for p in locations]}

    def register(self, basename, meta: Meta):
        """Tell the metadata agent what a file is."""
        self.catalog[basename] = meta

    def preload(self, path, meta: Meta, added_at=None, size=1):
        """Put a file in the library as if Plex added it before the bench started."""
        with self._lock:
            section = self._section_for_path(path)
            if section is None:
                raise ValueError(f'{path} is not under any library location')
            self._index_file(section, path, size, meta, added_at or self.clock.time())

    @property
    def url(self):
        return f'http://127.0.0.1:{self._server.server_address[1]}'

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                fake._handle(self, 'GET')

            def do_PUT(self):
                fake._handle(self, 'PUT')

            def do_POST(self):
                fake._handle(self, 'POST')

            def do_DELETE(self):
                fake._handle(self, 'DELETE')

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.05},
                                        daemon=True, name='fake-plex')
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # --- simulated time ----------------------------------------------------

    def advance(self, now):
        """Run the scanner up to simulated time ``now``."""
        with self._lock:
            while True:
                if self.current_scan is None:
                    if not self.scan_queue or self.behavior.scanner_stuck:
                        return
                    job = self.scan_queue.popleft()
                    job.started_at = max(job.requested_at, self.scan_done_at or job.requested_at)
                    self.current_scan = job
                    self.scan_done_at = job.started_at + self._scan_cost(job)
                if self.behavior.scanner_stuck or self.scan_done_at > now:
                    return
                job, done = self.current_scan, self.scan_done_at
                self._run_scan(job, done)
                self.scans_completed.append({'path': job.path, 'section': job.section_key,
                                             'requested_at': job.requested_at, 'done_at': done})
                self.current_scan = None

    def scan_backlog(self):
        with self._lock:
            return len(self.scan_queue) + (1 if self.current_scan else 0)

    def _scan_cost(self, job):
        if job.path is None:
            return self.behavior.full_scan_s
        return self.behavior.scan_overhead_s + self.behavior.scan_per_file_s * len(self._video_files(job.path))

    def _video_files(self, root):
        found = []
        if os.path.isfile(root):
            return [root] if root.lower().endswith(VIDEO_EXTS) else []
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.lower().endswith(VIDEO_EXTS):
                    found.append(os.path.join(dirpath, name))
        return sorted(found)

    def _run_scan(self, job, done_at):
        section = self.sections.get(job.section_key)
        if section is None:
            return
        roots = [job.path] if job.path else section['locations']
        added_at = done_at + self.behavior.index_delay_s
        indexed = {p for it in self.items.values() for p, _ in it.files}
        for root in roots:
            # Plex ignores a path outside the section's locations.
            if not any(_is_under(root, loc) for loc in section['locations']):
                continue
            for path in self._video_files(root):
                name = os.path.basename(path)
                if path in indexed or name in self.behavior.never_index:
                    continue
                meta = self.catalog.get(name)
                if meta is None:
                    if self.behavior.unmatched_files == 'skip':
                        continue
                    meta = Meta(type='movie' if section['type'] == 'movie' else 'episode',
                                title=os.path.splitext(name)[0])
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                self._index_file(section, path, size, meta, added_at, matched=name in self.catalog)

    def _index_file(self, section, path, size, meta, added_at, matched=True):
        added_at = int(added_at)
        if meta.type == 'movie':
            item = self._find(section['key'], 'movie', meta) if matched else None
            if item is None:
                item = self._new_item('movie', section['key'], meta.title, meta.year, meta, matched, added_at)
            item.files.append((path, size))
            item.updated_at = added_at
            return item
        show = self._find(section['key'], 'show', meta) if matched else None
        if show is None:
            show = self._new_item('show', section['key'], meta.title, meta.year, meta, matched, added_at)
        season = next((i for i in self.items.values() if i.type == 'season' and i.parent_key == show.rating_key
                       and i.index == meta.season), None)
        if season is None:
            season = self._new_item('season', section['key'], f'Season {meta.season}', None, None, matched, added_at)
            season.index, season.parent_key = meta.season, show.rating_key
        episode = next((i for i in self.items.values() if i.type == 'episode' and i.parent_key == season.rating_key
                        and i.index == meta.episode), None)
        if episode is None:
            episode = self._new_item('episode', section['key'], meta.episode_title or f'Episode {meta.episode}',
                                     None, None, matched, added_at)
            episode.index, episode.parent_key, episode.grandparent_key = meta.episode, season.rating_key, show.rating_key
        episode.files.append((path, size))
        episode.resolution = meta.resolution
        for it in (episode, season, show):
            it.updated_at = added_at
        return episode

    def _find(self, section_key, type_, meta):
        for it in self.items.values():
            if it.section_key != section_key or it.type != type_:
                continue
            if meta.imdb_id and f'imdb://{meta.imdb_id}' in it.guids:
                return it
            if meta.tmdb_id and f'tmdb://{meta.tmdb_id}' in it.guids:
                return it
        return None

    def _new_item(self, type_, section_key, title, year, meta, matched, added_at):
        self._next_key += 1
        key = self._next_key
        item = Item(rating_key=key, type=type_, section_key=section_key, title=title, year=year,
                    added_at=added_at, updated_at=added_at)
        if meta is not None:
            item.resolution = meta.resolution
            if matched:
                item.guid = f'plex://{type_}/{uuid.uuid5(uuid.NAMESPACE_URL, meta.title + str(meta.year)).hex[:24]}'
                item.guids = [f'{scheme}://{value}' for scheme, value in
                              (('imdb', meta.imdb_id), ('tmdb', meta.tmdb_id), ('tvdb', meta.tvdb_id)) if value]
            else:
                item.guid = f'local://{key}'
        else:
            item.guid = f'plex://{type_}/{key}' if matched else f'local://{key}'
        self.items[key] = item
        return item

    def _section_for_path(self, path):
        for section in self.sections.values():
            if any(_is_under(path, loc) for loc in section['locations']):
                return section
        return None

    # --- queries -----------------------------------------------------------

    def indexed_paths(self):
        with self._lock:
            return {p for it in self.items.values() for p, _ in it.files}

    def find_file(self, basename):
        with self._lock:
            for it in self.items.values():
                for p, _ in it.files:
                    if os.path.basename(p) == basename:
                        return it
        return None

    def count(self, pattern):
        """How many requests matched a path regex."""
        return sum(1 for r in self.requests if re.search(pattern, r['path']))

    # --- HTTP --------------------------------------------------------------

    def _handle(self, h, method):
        split = urlsplit(h.path)
        path = split.path.rstrip('/') or '/'
        query = {k: v[-1] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
        for header in ('X-Plex-Container-Start', 'X-Plex-Container-Size'):
            if h.headers.get(header) is not None and header not in query:
                query[header] = h.headers[header]
        now = self.clock.time()
        record = {'t': now, 'method': method, 'path': path, 'query': query}
        with self._lock:
            self.requests.append(record)
            b = self.behavior
            latency = sum(s for pat, s in b.latency_s.items() if re.search(pat, path))
            fault = None if not b.down else Fault('.*')
            if fault is None:
                fault = next((f for f in b.faults if f.matches(path, now)), None)
                if fault is not None and fault.times is not None:
                    fault.times -= 1
        if latency:
            # A slow Plex blocks the caller; in simulated time that's the clock moving.
            self.clock.advance(latency)
        if fault is not None:
            record['fault'] = fault.kind
            if fault.kind == 'drop':
                h.close_connection = True
                try:
                    h.connection.shutdown(2)
                except OSError:
                    pass
                return
            return self._send(h, 500 if fault.kind == 'error' else 401, '<html>fault</html>', 'text/html')
        token = h.headers.get('X-Plex-Token') or query.get('X-Plex-Token')
        if token != self.token:
            record['status'] = 401
            return self._send(h, 401, '<html>Unauthorized</html>', 'text/html')
        want_json = 'application/json' in (h.headers.get('Accept') or '')
        try:
            status, container = self._route(method, path, query)
        except _HTTPError as exc:
            record['status'] = exc.status
            return self._send(h, exc.status, '<html>Internal Server Error</html>', 'text/html')
        except Exception as exc:  # a bug in the fake shouldn't look like Plex
            record['status'] = 599
            return self._send(h, 500, f'fake plex error: {exc!r}', 'text/plain')
        record['status'] = status
        if status == 404:
            self.unhandled.append(f'{method} {h.path}')
        body = _to_json(container) if want_json else _to_xml(container)
        self._send(h, status, body, 'application/json' if want_json else 'text/xml;charset=utf-8')

    def _send(self, h, status, body, ctype):
        data = body.encode('utf-8')
        h.send_response(status)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _route(self, method, path, q):
        with self._lock:
            if path == '/' or path == '/identity':
                return 200, _mc({'friendlyName': 'FakePlex', 'machineIdentifier': self.machine_id,
                                 'version': '1.41.3.9314', 'platform': 'Linux', 'myPlex': '0',
                                 'myPlexSigninState': 'none', 'transcoderActiveVideoSessions': '0'})
            if path == '/library':
                return 200, _mc({'title1': 'Plex Library'}, [('Directory', {'key': 'sections', 'title': 'Library Sections'}, [])])
            if path == '/library/sections':
                return 200, _mc({}, [self._section_node(s) for s in self.sections.values()])
            if path == '/activities':
                acts = []
                if self.current_scan or self.scan_queue:
                    title = self.sections.get((self.current_scan or self.scan_queue[0]).section_key, {}).get('title', '')
                    acts.append(('Activity', {'uuid': uuid.uuid4().hex, 'type': 'library.update.section',
                                              'title': f'Scanning {title}', 'progress': '0', 'cancellable': '0'}, []))
                return 200, _mc({}, acts)
            m = re.fullmatch(r'/library/sections/(\w+)(/.*)?', path)
            if m:
                return self._route_section(m.group(1), m.group(2) or '', q)
            m = re.fullmatch(r'/library/metadata/(\d+)(/children|/allLeaves)?', path)
            if m:
                return self._route_metadata(method, int(m.group(1)), m.group(2) or '', q)
            if path == '/library/recentlyAdded':
                return 200, self._recently_added(None, q)
            return 404, _mc({})

    def _route_section(self, key, rest, q):
        section = self.sections.get(key)
        if section is None:
            return 404, _mc({})
        if rest == '':
            return 200, _mc({}, [self._section_node(section)])
        if rest == '/refresh':
            return 200, self._request_scan(key, q.get('path'))
        if rest == '/emptyTrash':
            return 200, _mc({})
        if rest == '/recentlyAdded':
            return 200, self._recently_added(key, q)
        if rest in ('/all', '/collections'):
            if q.get('includeMeta') == '1':
                return 200, self._filter_meta(section) if rest == '/all' else _mc({'size': '0'})
            if rest == '/collections':
                return 200, _mc({'size': '0'})
            return 200, self._section_all(section, q)
        return 404, _mc({})

    def _request_scan(self, section_key, path):
        path = os.path.normpath(unquote(path)) if path else None
        now = self.clock.time()
        b = self.behavior
        entry = {'t': now, 'section': section_key, 'path': path, 'accepted': True}
        self.scans_requested.append(entry)
        if b.coalesce_queued_scans and any(j.section_key == section_key and j.path == path for j in self.scan_queue):
            entry['accepted'] = False
            entry['reason'] = 'coalesced'
        elif b.max_queue is not None and len(self.scan_queue) >= b.max_queue:
            entry['accepted'] = False
            entry['reason'] = 'queue full'
        else:
            self.scan_queue.append(ScanJob(section_key, path, now))
            self.max_queue_seen = max(self.max_queue_seen, len(self.scan_queue))
            self.advance(now)  # an idle scanner starts on it straight away
        return _mc({})

    def _section_all(self, section, q):
        type_num = q.get('type')
        want = {'1': 'movie', '2': 'show', '3': 'season', '4': 'episode'}.get(type_num)
        want = want or ('movie' if section['type'] == 'movie' else 'show')
        items = [i for i in self.items.values() if i.section_key == section['key'] and i.type == want]
        if 'file' in q:
            if section['type'] == 'show' and type_num != '4':
                raise _HTTPError(500)
            needle = q['file'].lower()
            items = [i for i in self._with_files(items) if any(needle in p.lower() for p, _ in i.files)]
        if 'guid' in q:
            wanted = {g.strip() for g in q['guid'].split(',')}
            # Only the item's own guid: imdb:// and tmdb:// tags never match.
            items = [i for i in items if i.guid in wanted]
        if 'title' in q:
            needle = q['title'].lower()
            items = [i for i in items if needle in i.title.lower()]
        items.sort(key=lambda i: i.rating_key)
        start = int(q.get('X-Plex-Container-Start', 0) or 0)
        size = q.get('X-Plex-Container-Size')
        total = len(items)
        if size is not None:
            items = items[start:start + int(size)]
        return _mc({'size': str(len(items)), 'totalSize': str(total), 'offset': str(start)},
                   [self._item_node(i) for i in items])

    def _with_files(self, items):
        # A show "has" its episodes' files for ?file= on a show search.
        out = []
        for i in items:
            if i.type == 'show':
                eps = [e for e in self.items.values() if e.type == 'episode' and e.grandparent_key == i.rating_key]
                files = [f for e in eps for f in e.files]
                if files:
                    out.append(Item(**{**i.__dict__, 'files': files}))
                    continue
            out.append(i)
        return out

    def _recently_added(self, section_key, q):
        size = int(q.get('X-Plex-Container-Size', 50) or 50)
        pool = [i for i in self.items.values()
                if (section_key is None or i.section_key == section_key) and i.type in ('movie', 'episode')]
        pool.sort(key=lambda i: (i.added_at, i.rating_key), reverse=True)
        entries, seen_seasons = [], set()
        for it in pool:
            if it.type == 'episode' and self.behavior.group_seasons:
                siblings = [e for e in pool if e.type == 'episode' and e.parent_key == it.parent_key
                            and abs(e.added_at - it.added_at) <= 3600]
                if len(siblings) > 1:
                    if it.parent_key in seen_seasons:
                        continue
                    seen_seasons.add(it.parent_key)
                    season = self.items[it.parent_key]
                    entries.append((max(e.added_at for e in siblings), season))
                    continue
            entries.append((it.added_at, it))
        entries = entries[:size]
        return _mc({'size': str(len(entries))}, [self._item_node(i, added_at=a) for a, i in entries])

    def _route_metadata(self, method, key, rest, q):
        item = self.items.get(key)
        if item is None:
            return 404, _mc({})
        if method == 'DELETE':
            self._delete(item)
            return 200, _mc({})
        if rest == '':
            return 200, _mc({}, [self._item_node(item)])
        if rest == '/children':
            kids = [i for i in self.items.values() if i.parent_key == key]
        else:
            kids = [i for i in self.items.values() if i.type == 'episode' and i.grandparent_key == key]
        kids.sort(key=lambda i: (i.index or 0))
        return 200, _mc({'size': str(len(kids))}, [self._item_node(i) for i in kids])

    def _delete(self, item):
        doomed = {item.rating_key}
        doomed |= {i.rating_key for i in self.items.values() if i.parent_key in doomed or i.grandparent_key in doomed}
        for k in doomed:
            self.items.pop(k, None)

    # --- nodes ---------------------------------------------------------------

    def _section_node(self, s):
        attrs = {'key': s['key'], 'title': s['title'], 'type': s['type'], 'allowSync': '1',
                 'agent': 'tv.plex.agents.movie' if s['type'] == 'movie' else 'tv.plex.agents.series',
                 'scanner': 'Plex Movie' if s['type'] == 'movie' else 'Plex TV Series',
                 'language': 'en-US', 'uuid': str(uuid.uuid5(uuid.NAMESPACE_URL, s['key'])),
                 'refreshing': '1' if (self.current_scan and self.current_scan.section_key == s['key']) else '0'}
        locs = [('Location', {'id': str(n), 'path': p}, []) for n, p in enumerate(s['locations'], 1)]
        return ('Directory', attrs, locs)

    def _item_node(self, it, added_at=None):
        a = {'ratingKey': str(it.rating_key), 'type': it.type, 'title': it.title, 'guid': it.guid,
             'librarySectionID': it.section_key, 'addedAt': str(added_at or it.added_at),
             'updatedAt': str(it.updated_at)}
        if it.year:
            a['year'] = str(it.year)
        if it.index is not None:
            a['index'] = str(it.index)
        if it.type in ('show', 'season'):
            a['key'] = f'/library/metadata/{it.rating_key}/children'
        else:
            a['key'] = f'/library/metadata/{it.rating_key}'
        if it.type == 'season':
            show = self.items.get(it.parent_key)
            a['parentRatingKey'] = str(it.parent_key)
            a['parentTitle'] = show.title if show else ''
            a['parentKey'] = f'/library/metadata/{it.parent_key}'
        if it.type == 'episode':
            season = self.items.get(it.parent_key)
            show = self.items.get(it.grandparent_key)
            a.update({'parentRatingKey': str(it.parent_key), 'grandparentRatingKey': str(it.grandparent_key),
                      'parentIndex': str(season.index if season else ''),
                      'grandparentTitle': show.title if show else '',
                      'parentKey': f'/library/metadata/{it.parent_key}',
                      'grandparentKey': f'/library/metadata/{it.grandparent_key}'})
        children = []
        for n, (path, size) in enumerate(it.files, 1):
            part = ('Part', {'id': f'{it.rating_key}{n}', 'key': f'/library/parts/{it.rating_key}{n}/file.mkv',
                             'file': path, 'size': str(size), 'container': 'mkv'}, [])
            children.append(('Media', {'id': f'{it.rating_key}{n}', 'videoResolution': it.resolution,
                                       'container': 'mkv'}, [part]))
        children += [('Guid', {'id': g}, []) for g in it.guids]
        tag = 'Video' if it.type in ('movie', 'episode') else 'Directory'
        return (tag, a, children)

    def _filter_meta(self, section):
        types = ['movie'] if section['type'] == 'movie' else ['show', 'season', 'episode']
        nums = {'movie': 1, 'show': 2, 'season': 3, 'episode': 4}
        type_nodes = []
        for t in types:
            fields = [('Field', {'key': k if t == types[0] else f'{t}.{k}', 'title': k, 'type': ft}, [])
                      for k, ft in (('title', 'string'), ('year', 'integer'), ('file', 'string'))]
            type_nodes.append(('Type', {'key': f'/library/sections/{section["key"]}/all?type={nums[t]}', 'type': t,
                                        'title': t.capitalize(), 'active': '1' if t == types[0] else '0'}, fields))
        ops = {'string': ['=', '!=', '==', '!==', '<=', '>='], 'integer': ['=', '!=', '>>=', '<<='],
               'guid': ['='], 'tag': ['=', '!='], 'boolean': ['=', '!='], 'date': ['<<=', '>>=']}
        field_types = [('FieldType', {'type': t}, [('Operator', {'key': o, 'title': o}, []) for o in os_])
                       for t, os_ in ops.items()]
        return _mc({'size': '0'}, [('Meta', {}, type_nodes + field_types)])


class _HTTPError(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _is_under(path, root):
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip('/') + '/')


def _mc(attrs, children=None):
    return ('MediaContainer', attrs, children or [])


def _to_xml(node):
    tag, attrs, children = node
    a = ''.join(f' {k}={quoteattr(str(v))}' for k, v in attrs.items())
    if not children:
        return f'<{tag}{a}/>'
    return f'<{tag}{a}>' + ''.join(_to_xml(c) for c in children) + f'</{tag}>'


# Plex's JSON puts child elements in lists keyed by tag; items are "Metadata".
_JSON_KEYS = {'Video': 'Metadata', 'Directory': None, 'Media': 'Media', 'Part': 'Part', 'Guid': 'Guid',
              'Location': 'Location', 'Activity': 'Activity', 'Meta': 'Meta', 'Type': 'Type',
              'Field': 'Field', 'FieldType': 'FieldType', 'Operator': 'Operator'}


def _to_json(node):
    def conv(n):
        tag, attrs, children = n
        out = {k: _json_value(k, v) for k, v in attrs.items()}
        for c in children:
            key = _JSON_KEYS.get(c[0], c[0])
            if c[0] == 'Directory':
                # Sections stay "Directory"; shows/seasons are "Metadata".
                key = 'Metadata' if c[1].get('ratingKey') else 'Directory'
            out.setdefault(key, []).append(conv(c))
        return out
    return json.dumps({'MediaContainer': conv(node)})


# Plex's JSON has these as numbers; ratingKey, key and the rest stay strings.
_JSON_INTS = {'addedAt', 'updatedAt', 'year', 'index', 'parentIndex', 'size', 'librarySectionID',
              'id', 'totalSize', 'offset', 'progress'}


def _json_value(key, v):
    if key in _JSON_INTS and re.fullmatch(r'-?\d+', str(v)):
        return int(v)
    return v
