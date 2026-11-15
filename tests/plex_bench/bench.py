"""The Plex-mode bench: cli_debrid's real Checking queue and Plex tasks, run on a
simulated clock against a simulated Plex (fake_plex.py) and a fake usenet
provider whose jobs land as real files in a temp "mount".

    bench = PlexBench(...)                # see conftest.py's plex_bench fixture
    bench.add_movie('Dune', 2021, 'tt1160419', download_s=120)
    bench.run(hours=2)
    print(bench.report())

What runs for real: CheckingQueue.process (progress polling, NZB file resolve,
targeted Plex scan, the Checking timeout, the stuck-Plex hold),
task_check_plex_files, get_and_add_recent_collected_from_plex / add_collected_items,
task_reconcile_queues, plex_update_item, plexapi and aiohttp.

What's faked: the usenet provider (FakeUsenet), Plex (FakePlex), the clock, and
anything that would leave the machine (NetworkGuard turns it into an error and
records it, so a test can fail on unexpected outbound calls).
"""

import logging
import os
import re
import socket
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

from tests.plex_bench.clock import SimClock
from tests.plex_bench.fake_plex import FakePlex, Meta

# Tasks that touch Plex mode's Checking -> Collected path, and the order they
# run in when due at the same moment.
BENCH_TASKS = ('Checking', 'task_check_plex_files', 'task_plex_full_scan', 'task_reconcile_queues')


# --- usenet provider --------------------------------------------------------

@dataclass
class Job:
    job_id: str
    name: str
    category: str
    files: List[tuple]               # (relative path, size)
    submitted_at: float
    download_s: float
    fail: bool = False
    visible_while_downloading: bool = False
    removed: bool = False
    materialized: bool = False

    def progress(self, now):
        if self.download_s <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - self.submitted_at) / self.download_s))


class FakeUsenet:
    """Stands in for usenet.climount_client.CliMountClient (and get_usenet_client()).

    A job's files appear under <mount>/<category>/<job name>/ when it completes
    (or at submit, with visible_while_downloading, like a streaming mount).
    """

    def __init__(self, bench):
        self.bench = bench
        self.jobs: Dict[str, Job] = {}
        self.calls = Counter()
        self.removed: List[tuple] = []

    # provider API used by the Checking queue ---------------------------------

    def is_enabled(self):
        return True

    def get_job_status(self, job_id):
        self.calls['get_job_status'] += 1
        job = self.jobs.get(str(job_id))
        if job is None or job.removed:
            return None
        now = self.bench.clock.time()
        self._materialize(job, now)
        p = job.progress(now)
        if job.fail and p >= 1:
            return {'state': 'failed', 'raw': {'state': 'error', 'status': 'failed', 'progress': p, 'name': job.name}}
        done = p >= 1
        raw = {'state': 'pausedUP' if done else 'downloading', 'status': 'downloaded' if done else 'downloading',
               'progress': p, 'is_complete': done, 'name': job.name}
        return {'state': 'completed' if done else 'downloading', 'progress': p, 'name': job.name, 'raw': raw}

    def get_nzb_file_info(self, job_name, season=None, episode=None, fast_check=False, include_size=False):
        self.calls['get_nzb_file_info'] += 1
        found = self._folder(job_name)
        if not found:
            return None
        folder, files = found
        if not files:
            return (folder, None, None) if include_size else (folder, None)
        from debrid.common import filter_unwanted_video_files, pick_best_video_file
        files = filter_unwanted_video_files(files)
        best = pick_best_video_file(files, season=season, episode=episode) or max(files, key=lambda f: f[1])
        return (folder, best[0], best[1]) if include_size else (folder, best[0])

    def get_nzb_folder_all_files(self, job_name):
        self.calls['get_nzb_folder_all_files'] += 1
        found = self._folder(job_name)
        if not found:
            return None
        from debrid.common import filter_unwanted_video_files
        folder, files = found
        return folder, sorted(name for name, _ in filter_unwanted_video_files(files))

    def remove_nzb(self, info_hash, entry_name=''):
        self.calls['remove_nzb'] += 1
        self.removed.append((info_hash, entry_name))
        job = self.jobs.get(str(info_hash))
        if job is not None:
            job.removed = True
            path = os.path.join(self.bench.mount, job.category, job.name)
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
        return True

    def register_cli_ids(self, *a, **k):
        self.calls['register_cli_ids'] += 1

    def register_cli_ids_for_item(self, *a, **k):
        self.calls['register_cli_ids_for_item'] += 1

    def push_tags_for_item(self, *a, **k):
        self.calls['push_tags_for_item'] += 1

    def __getattr__(self, name):
        # Anything else the app calls on the client: record it, return nothing.
        if name.startswith('_'):
            raise AttributeError(name)

        def _unhandled(*a, **k):
            self.calls[f'unhandled:{name}'] += 1
            return None
        return _unhandled

    # bench side -----------------------------------------------------------

    def tick(self, now):
        for job in self.jobs.values():
            self._materialize(job, now)

    def _materialize(self, job, now):
        if job.materialized or job.removed or job.fail:
            return
        if job.progress(now) < 1 and not job.visible_while_downloading:
            return
        base = os.path.join(self.bench.mount, job.category, job.name)
        for rel, size in job.files:
            path = os.path.join(base, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as fh:
                fh.truncate(size)  # sparse: the size is real, the disk use isn't
        job.materialized = True

    def _folder(self, job_name):
        norm = _norm(job_name)
        for category in sorted(os.listdir(self.bench.mount)):
            cat_dir = os.path.join(self.bench.mount, category)
            if not os.path.isdir(cat_dir):
                continue
            for folder in os.listdir(cat_dir):
                if _norm(folder) == norm:
                    files = []
                    for dirpath, _d, names in os.walk(os.path.join(cat_dir, folder)):
                        for n in names:
                            files.append((n, os.path.getsize(os.path.join(dirpath, n))))
                    return folder, files
        return None


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# --- network guard ------------------------------------------------------------

class NetworkGuard:
    """Refuse every connection that isn't to this machine and remember who tried."""

    def __init__(self):
        self.blocked: List[str] = []

    def install(self, monkeypatch):
        guard = self
        real_connect = socket.socket.connect
        real_getaddrinfo = socket.getaddrinfo

        def _local(host):
            return host in ('127.0.0.1', 'localhost', '::1') or str(host).startswith('127.')

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(address, tuple) and not _local(host):
                guard._block(f'connect {host}:{address[1]}')
            return real_connect(sock, address)

        def getaddrinfo(host, *a, **k):
            if host is not None and not _local(host):
                guard._block(f'resolve {host}')
            return real_getaddrinfo(host, *a, **k)

        monkeypatch.setattr(socket.socket, 'connect', connect)
        monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
        return self

    def _block(self, what):
        import traceback
        frames = [f for f in traceback.extract_stack()[:-3]
                  if '/site-packages/' not in f.filename and 'plex_bench' not in f.filename]
        where = ' <- '.join(f'{os.path.basename(f.filename)}:{f.lineno}' for f in frames[-3:][::-1])
        self.blocked.append(f'{what} ({where})')
        raise OSError(f'plex bench: outbound network blocked: {what}')


# --- log capture --------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self, clock):
        super().__init__(logging.DEBUG)
        self.clock = clock
        self.records: List[tuple] = []

    def emit(self, record):
        try:
            self.records.append((self.clock.time(), record.levelname, record.getMessage()))
        except Exception:
            pass


# --- the bench ------------------------------------------------------------------

@dataclass
class Release:
    """One thing the bench downloads: a movie, an episode, or a season pack."""
    job_id: str
    name: str
    item_ids: List[int]
    files: List[str]
    category: str
    submitted_at: float
    meta: List[Meta] = field(default_factory=list)


class PlexBench:
    def __init__(self, root, monkeypatch, app_settings, *, clock=None, plex_settings=None,
                 checking_queue_period=3600, sync_symlink_plex_url=True, layout='nested'):
        self.root = str(root)
        self.mp = monkeypatch
        self.settings = app_settings
        self.clock = (clock or SimClock()).install(monkeypatch)
        self.start_time = self.clock.time()
        self.layout = layout
        self.mount = os.path.join(self.root, 'mnt')
        self.categories = {'movie': 'movies', 'episode': 'tv'}
        for cat in self.categories.values():
            os.makedirs(os.path.join(self.mount, cat), exist_ok=True)

        self.plex = FakePlex(self.clock, sections=[
            dict(key=1, title='Movies', type='movie', locations=[os.path.join(self.mount, 'movies')]),
            dict(key=2, title='Shows', type='show', locations=[os.path.join(self.mount, 'tv')]),
        ]).start()
        self.usenet = FakeUsenet(self)
        self.net = NetworkGuard().install(monkeypatch)
        self.releases: List[Release] = []
        self.task_runs = Counter()
        self.task_wall = Counter()  # real seconds per task, to keep the bench itself fast
        self.task_errors: List[str] = []
        self.history: Dict[int, List[tuple]] = defaultdict(list)  # item id -> [(t, state)]
        self._last_state: Dict[int, str] = {}
        self._job_seq = 0

        self._configure(plex_settings or {}, checking_queue_period, sync_symlink_plex_url)
        self._patch_app()
        self._capture_logs()
        self._build_runner()

    # --- setup -----------------------------------------------------------------

    def _configure(self, plex_settings, checking_queue_period, sync_symlink_plex_url):
        s = self.settings
        s['File Management'] = {
            'file_collection_management': 'Plex',
            'plex_url_for_symlink': self.plex.url if sync_symlink_plex_url else '',
            'plex_token_for_symlink': self.plex.token if sync_symlink_plex_url else '',
        }
        s['Plex'] = {
            'url': self.plex.url, 'token': self.plex.token,
            'movie_libraries': 'Movies', 'shows_libraries': 'Shows',
            'mounted_file_location': self.mount,
            'update_plex_on_file_discovery': False,
            'disable_plex_library_checks': False,
        }
        s['Plex'].update(plex_settings)
        s['Usenet Provider'] = {'enabled': True, 'provider': 'climount', 'url': 'http://127.0.0.1:9',
                                'mounted_file_location': self.mount,
                                'ffprobe_all_nzbs': False}
        s['Debrid Provider'] = {'provider': '', 'api_key': ''}
        s['Debug'] = {'checking_queue_period': checking_queue_period,
                      'enable_unmatched_items_check': False}
        s['Scraping'] = {'versions': {'Default': {}}}

    def _patch_app(self):
        mp, usenet = self.mp, self.usenet
        import usenet as usenet_pkg
        from usenet import climount_client
        mp.setattr(usenet_pkg, 'get_usenet_client', lambda: usenet)
        mp.setattr(usenet_pkg, 'reset_usenet_client', lambda: None)
        mp.setattr(climount_client, 'get_climount_client', lambda: usenet)
        mp.setattr(climount_client, 'reset_climount_client', lambda: None, raising=False)

        # Fresh singletons and module state for this run.
        import debrid
        from queues.checking_queue import CheckingQueue
        from queues.queue_manager import QueueManager
        from queues import run_program
        from utilities import plex_functions, plex_health
        mp.setattr(debrid, '_provider_instance', None, raising=False)
        mp.setattr(debrid, '_provider_list', None, raising=False)
        mp.setattr(CheckingQueue, '_instance', None)
        mp.setattr(QueueManager, '_instance', None)
        mp.setattr(run_program.ProgramRunner, '_instance', None)
        mp.setattr(run_program, '_last_plex_recent_scan_time', None)
        mp.setattr(plex_functions, '_plex_mode_scans', {})
        mp.setattr(plex_functions, '_scanning_cache', {'at': 0.0, 'value': None})
        # fetch_data waits RETRY_DELAY real seconds between attempts; simulated
        # time doesn't move during a task, so waiting buys nothing here.
        mp.setattr(plex_functions, 'RETRY_DELAY', 0)
        plex_health._reset_for_tests()

        # Threads the Checking queue starts for its Plex scan run inline, so a run
        # is repeatable. Anything else still gets a real thread.
        inline = {'_do_plex_scan'}
        real_thread = threading.Thread
        bench = self

        class _InlineThread(real_thread):
            def start(self):
                target = getattr(self, '_target', None)
                if target is not None and getattr(target, '__name__', '') in inline:
                    try:
                        target(*self._args, **self._kwargs)
                    except Exception as exc:  # the thread would have swallowed it too
                        bench.task_errors.append(f'{target.__name__}: {exc!r}')
                    return
                return super().start()

        mp.setattr(threading, 'Thread', _InlineThread)

    def _capture_logs(self):
        self.log = _Capture(self.clock)
        root = logging.getLogger()
        root.addHandler(self.log)
        self.mp.setattr(root, 'level', logging.DEBUG)

    def _build_runner(self):
        import database  # noqa: F401  (app import order)
        from queues.run_program import ProgramRunner
        self.runner = ProgramRunner()
        # The bench is the scheduler. Its APScheduler is built but must never run.
        assert not getattr(self.runner.scheduler, 'running', False), 'ProgramRunner scheduler is running'
        self.qm = self.runner.queue_manager
        self.checking = self.qm.queues['Checking']
        self.intervals = {t: self.runner.task_intervals.get(t) for t in BENCH_TASKS}
        self.enabled = [t for t in BENCH_TASKS if t in self.runner.enabled_tasks]
        self.next_run = {t: self.start_time + 1 for t in self.enabled}

    def close(self):
        self.plex.stop()
        logging.getLogger().removeHandler(self.log)

    # --- content ---------------------------------------------------------------

    def add_movie(self, title, year, imdb_id, tmdb_id=None, *, download_s=60, release=None,
                  size=4_000_000_000, fail=False, register=True, version='Default', at=None):
        name = release or f'{title.replace(" ", ".")}.{year}.1080p.WEB-DL.H264-BENCH'
        fname = f'{name}.mkv'
        meta = Meta('movie', title, year, imdb_id, tmdb_id)
        if register:
            self.plex.register(fname, meta)
        row = dict(type='movie', title=title, year=year, imdb_id=imdb_id, tmdb_id=tmdb_id, version=version)
        return self._submit(name, 'movie', [(fname, size)], [row], [meta], download_s, fail, at)

    def add_episodes(self, show, year, imdb_id, season, episodes, tvdb_id=None, tmdb_id=None, *,
                     download_s=60, release=None, size=1_500_000_000, fail=False, register=True,
                     version='Default', at=None):
        """One NZB with these episodes: a single episode, or a pack when several."""
        episodes = list(episodes)
        dotted = show.replace(' ', '.')
        if release:
            name = release
        elif len(episodes) == 1:
            name = f'{dotted}.S{season:02d}E{episodes[0]:02d}.1080p.WEB-DL.H264-BENCH'
        else:
            name = f'{dotted}.S{season:02d}.1080p.WEB-DL.H264-BENCH'
        files, rows, metas = [], [], []
        for ep in episodes:
            fname = f'{dotted}.S{season:02d}E{ep:02d}.1080p.WEB-DL.H264-BENCH.mkv'
            meta = Meta('episode', show, year, imdb_id, tmdb_id, tvdb_id, season, ep)
            if register:
                self.plex.register(fname, meta)
            files.append((fname, size))
            metas.append(meta)
            rows.append(dict(type='episode', title=show, year=year, imdb_id=imdb_id, tmdb_id=tmdb_id,
                             season_number=season, episode_number=ep, episode_title=f'Episode {ep}',
                             version=version))
        return self._submit(name, 'episode', files, rows, metas, download_s, fail, at)

    def _submit(self, name, kind, files, rows, metas, download_s, fail, at):
        """An NZB that the Adding queue has just handed to the provider: the job
        exists and its items are in Checking."""
        self._job_seq += 1
        job_id = f'{self._job_seq:08x}benchjob'
        now = at if at is not None else self.clock.time()
        category = self.categories[kind]
        self.usenet.jobs[job_id] = Job(job_id, name, category, files, now, download_s, fail)
        from database.core import get_db_connection
        conn = get_db_connection()
        ids = []
        try:
            ts = _ts(now)
            for row in rows:
                data = dict(row)
                data.update(state='Checking', filled_by_torrent_id=f'nzb:{job_id}', filled_by_title=name,
                            filled_by_file=name, filled_by_magnet=f'https://indexer.invalid/getnzb/{job_id}',
                            original_scraped_torrent_title=name, last_updated=ts, release_date='2020-01-01',
                            content_source='bench', ghostlisted=0)
                cols = ', '.join(data)
                cur = conn.execute(f'INSERT INTO media_items ({cols}) VALUES ({", ".join("?" * len(data))})',
                                   list(data.values()))
                ids.append(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        rel = Release(job_id, name, ids, [f for f, _ in files], category, now, metas)
        self.releases.append(rel)
        for i in ids:
            self._note_state(i, 'Checking', now)
        return rel

    def preload_library(self, count, kind='movie', added_at=None):
        """Fill Plex with older items (they crowd recently-added like a real library)."""
        t = added_at or (self.clock.time() - 86400)
        for n in range(count):
            if kind == 'movie':
                name = f'Filler.Movie.{n:04d}.2001.1080p-OLD'
                path = os.path.join(self.mount, 'movies', name, f'{name}.mkv')
                meta = Meta('movie', f'Filler Movie {n}', 2001, f'tt9{n:07d}')
            else:
                name = f'Filler.Show.{n:04d}.S01E01.1080p-OLD'
                path = os.path.join(self.mount, 'tv', name, f'{name}.mkv')
                meta = Meta('episode', f'Filler Show {n}', 2001, f'tt8{n:07d}', None, None, 1, 1)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'wb').close()
            self.plex.preload(path, meta, added_at=t)

    # --- running ---------------------------------------------------------------

    def run(self, seconds=0, *, minutes=0, hours=0, until=None, step=None):
        """Run the scheduler for a while in simulated time.

        ``until`` is a callable checked after every task run; the run stops early
        once it returns True. Returns the simulated seconds that passed.
        """
        end = self.clock.time() + seconds + minutes * 60 + hours * 3600
        start = self.clock.time()
        while True:
            due_t = min(self.next_run.values()) if self.next_run else end
            if due_t > end:
                self._advance_to(end)
                break
            self._advance_to(due_t)
            for task in self.enabled:
                if self.next_run[task] <= self.clock.time():
                    self._run_task(task)
                    # APScheduler: next fire is a whole interval after the scheduled
                    # time; runs missed while this one was busy are skipped.
                    nxt = self.next_run[task] + self.intervals[task]
                    while nxt <= self.clock.time():
                        nxt += self.intervals[task]
                    self.next_run[task] = nxt
            self._sample_states()
            if until is not None and until():
                break
        return self.clock.time() - start

    def run_until_settled(self, max_hours=12):
        """Run until nothing is left in Checking (or max_hours passes)."""
        return self.run(hours=max_hours, until=lambda: not self.items_in('Checking'))

    def _advance_to(self, t):
        self.clock.set(t)
        now = self.clock.time()
        self.usenet.tick(now)
        self.plex.advance(now)

    def _run_task(self, task):
        self.task_runs[task] += 1
        began = _perf()
        try:
            if task == 'Checking':
                # The runner's task_update_queue_views reloads every queue from the
                # database every 30 s; here that happens right before each pass.
                self.checking.update()
                self.qm.process_checking(self.runner)
            else:
                getattr(self.runner, task)()
        except Exception as exc:
            self.task_errors.append(f'{task}: {exc!r}')
            logging.exception(f'[bench] {task} raised')
        self.task_wall[task] += _perf() - began
        # Plex keeps working while cli_debrid's task ran (latency moves the clock).
        self.plex.advance(self.clock.time())

    def _sample_states(self):
        now = self.clock.time()
        for row in self.rows():
            self._note_state(row['id'], row['state'], now)

    def _note_state(self, item_id, state, t):
        if self._last_state.get(item_id) != state:
            self._last_state[item_id] = state
            self.history[item_id].append((t, state))

    # --- reading results ---------------------------------------------------------

    def rows(self, ids=None):
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            rows = [dict(r) for r in conn.execute('SELECT * FROM media_items ORDER BY id')]
        finally:
            conn.close()
        if ids is not None:
            ids = set(ids)
            rows = [r for r in rows if r['id'] in ids]
        return rows

    def items_in(self, state):
        return [r for r in self.rows() if r['state'] == state]

    def state_of(self, release):
        return [r['state'] for r in self.rows(release.item_ids)]

    def time_to(self, item_id, state='Collected'):
        """Simulated seconds from entering Checking to first reaching ``state``, or None."""
        hist = self.history.get(item_id, [])
        start = next((t for t, s in hist if s == 'Checking'), None)
        end = next((t for t, s in hist if s == state), None)
        return None if start is None or end is None else end - start

    def logs(self, pattern, level=None):
        rx = re.compile(pattern)
        return [(t, lvl, msg) for t, lvl, msg in self.log.records
                if rx.search(msg) and (level is None or lvl == level)]

    def false_collections(self):
        """Items marked Collected whose file Plex never indexed."""
        indexed = {os.path.basename(p) for p in self.plex.indexed_paths()}
        out = []
        for r in self.items_in('Collected'):
            name = os.path.basename(r.get('location_on_disk') or r.get('filled_by_file') or '')
            if name not in indexed:
                out.append(r)
        return out

    def not_wanted_count(self):
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            total = 0
            for table in ('not_wanted_magnets', 'not_wanted_urls', 'not_wanted_nzb_guids'):
                if table in tables:
                    total += conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            return total
        finally:
            conn.close()

    def metrics(self):
        rows = self.rows()
        ids = [i for rel in self.releases for i in rel.item_ids]
        times = [t for t in (self.time_to(i) for i in ids) if t is not None]
        scans = Counter(e['path'] for e in self.plex.scans_requested)
        went_wanted = sum(1 for i in ids if any(s == 'Wanted' for _, s in self.history.get(i, [])))
        return {
            'simulated_hours': round((self.clock.time() - self.start_time) / 3600, 2),
            'items': len(ids),
            'collected': sum(1 for r in rows if r['id'] in ids and r['state'] == 'Collected'),
            'still_checking': sum(1 for r in rows if r['id'] in ids and r['state'] == 'Checking'),
            'sent_back_to_wanted': went_wanted,
            'false_collections': len([r for r in self.false_collections() if r['id'] in ids]),
            'time_to_collect_s': _summary(times),
            'plex_requests': len(self.plex.requests),
            'plex_scans_requested': len(self.plex.scans_requested),
            'plex_scans_completed': len(self.plex.scans_completed),
            'plex_max_scans_per_folder': max(scans.values()) if scans else 0,
            'plex_max_scan_queue': self.plex.max_queue_seen,
            'plex_unhandled_endpoints': sorted(set(self.plex.unhandled)),
            'recently_added_calls': self.plex.count(r'/recentlyAdded$'),
            'file_lookups': sum(1 for r in self.plex.requests if r['path'].endswith('/all') and 'file' in r['query']),
            'nzb_removed': len(self.usenet.removed),
            'not_wanted_entries': self.not_wanted_count(),
            'outbound_blocked': len(self.net.blocked),
            'task_runs': dict(self.task_runs),
            'task_wall_s': {k: round(v, 2) for k, v in self.task_wall.items()},
            'task_errors': len(self.task_errors),
            'errors_logged': len(self.logs('.', level='ERROR')),
        }

    def report(self):
        m = self.metrics()
        lines = ['Plex bench report', '=' * 40]
        for k, v in m.items():
            lines.append(f'{k:28} {v}')
        if self.net.blocked:
            lines.append('\noutbound calls blocked (first 10):')
            lines += [f'  {b}' for b in dict.fromkeys(self.net.blocked).keys()][:10]
        if self.task_errors:
            lines.append('\ntask errors:')
            lines += [f'  {e}' for e in self.task_errors[:10]]
        errors = self.logs('.', level='ERROR')
        if errors:
            lines.append('\nERROR log lines (first 10 distinct):')
            seen = list(dict.fromkeys(msg[:200] for _, _, msg in errors))
            lines += [f'  {msg}' for msg in seen[:10]]
        lines.append('\nper release:')
        for rel in self.releases:
            states = Counter(self.state_of(rel))
            ttc = [self.time_to(i) for i in rel.item_ids]
            ttc = [round(t / 60, 1) for t in ttc if t is not None]
            scans = sum(1 for e in self.plex.scans_requested if e['path'] and rel.name in e['path'])
            lines.append(f'  {rel.name[:60]:60} {dict(states)} collect_min={ttc[:3]} scans={scans}')
        return '\n'.join(lines)

    def dump_log(self, path, pattern=None):
        rx = re.compile(pattern) if pattern else None
        with open(path, 'w') as fh:
            for t, lvl, msg in self.log.records:
                if rx is None or rx.search(msg):
                    fh.write(f'+{(t - self.start_time) / 60:8.2f}m {lvl:7} {msg}\n')
        return path


def _perf():
    import time
    return time.perf_counter()


def _ts(t):
    from datetime import datetime
    return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')


def _summary(values):
    if not values:
        return None
    v = sorted(values)
    pick = lambda q: round(v[min(len(v) - 1, int(q * len(v)))] / 60, 1)
    return {'n': len(v), 'p50_min': pick(0.5), 'p90_min': pick(0.9), 'max_min': round(v[-1] / 60, 1)}
