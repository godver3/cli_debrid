# Plex-mode bench

Runs cli_debrid's real Plex-mode pipeline (Checking queue, targeted Plex scans,
recently-added collection, `task_check_plex_files`, reconciliation) against a
simulated Plex server, a fake usenet provider and a fake mount, on a simulated
clock. Hours of Checking-queue time take seconds, so the "stuck in Checking",
"slow scans" and "same release grabbed again" reports can be reproduced and
pinned as tests.

```bash
.venv/bin/python -m pytest -m plex_bench -rxX     # all bench scenarios, ~40 s
```

`xfail` tests are known bugs, and each one's reason explains it. They're
`strict`, so fixing one turns it red: remove the marker as part of the fix.

## What's real and what's faked

| Real (unchanged app code) | Faked |
|---|---|
| `CheckingQueue.process`, NZB file resolve, the Checking timeout, the stuck-Plex hold | the usenet provider (`FakeUsenet` in `bench.py`); job files are written as sparse files under `<tmp>/mnt/<category>/<job>/` when the download finishes |
| `plex_update_item`, `task_check_plex_files`, `get_and_add_recent_collected_from_plex`, `add_collected_items`, `task_reconcile_queues`, `task_plex_full_scan` | Plex (`fake_plex.py`): a real HTTP server on 127.0.0.1, so plexapi and aiohttp run unchanged |
| the database (real schema), settings lookups | the clock (`clock.py`): `time.time`, `time.sleep`, `datetime.now` |
| | the scheduler: `PlexBench.run()` runs the tasks above at the runner's own intervals, APScheduler-style (missed runs skipped) |

Anything that tries to leave the machine (TMDB, indexers, Trakt...) is refused
and listed in the report (`outbound_blocked`). A scenario that hits one either
stubs it or accepts it on purpose.

## Writing a scenario

```python
def test_something(plex_bench):
    bench = plex_bench(checking_queue_period=600)       # settings knobs: see PlexBench.__init__
    bench.plex.behavior.scan_overhead_s = 90             # Plex knobs: see fake_plex.Behavior
    movie = bench.add_movie('Dune', 2021, 'tt1160419', download_s=120)
    pack = bench.add_episodes('Severance', 2022, 'tt11280740', 1, range(1, 10), tvdb_id='371980')

    bench.run(hours=2)                                   # or run_until_settled()

    assert bench.state_of(movie) == ['Collected']
    print(bench.report())                                # metrics + per-release summary
    bench.dump_log('/tmp/bench.log')                     # the app's log, stamped with simulated minutes
```

Things to reach for:

- **Plex behavior** (`bench.plex.behavior`): scan speed (`scan_overhead_s`, `scan_per_file_s`,
  `full_scan_s`), `scanner_stuck`, `never_index` (files Plex rejects), `max_queue`,
  `coalesce_queued_scans`, per-endpoint `latency_s` (moves the clock, like a slow Plex blocking the caller),
  `faults` (`Fault(pattern, 'drop'|'error'|'unauthorized', times=, until=)`), `down`.
- **Content**: `add_movie`, `add_episodes` (one episode or a pack), `register=False` for a file
  Plex can't match, `fail=True` for a failed job, `preload_library(n)` for an existing library.
- **Results**: `state_of(release)`, `rows()`, `history[item_id]` (state changes with times),
  `time_to(item_id)`, `false_collections()`, `metrics()`, `logs(regex)`, `plex.requests`,
  `plex.scans_requested` / `scans_completed`, `usenet.removed`.

## Fidelity: unconfirmed against a real Plex

The fake follows plexapi and the app's own parsing, and hasn't been compared with
a live Plex Media Server. These assumptions change the conclusions and should be
checked against a throwaway PMS before relying on them:

1. Plex runs scan requests one at a time and does **not** merge a request
   identical to one already queued (`coalesce_queued_scans=False`).
2. `?file=<name>` matches a substring of the part path.
3. `guid=imdb://...` on `/library/sections/N/all` matches external Guid tags
   (with the new Plex agents it may only match `plex://` guids, in which case the
   GUID fallback in `task_check_plex_files` never matches anything in real life).
4. Section `recentlyAdded` groups several new episodes of a season into the season.
5. An empty section's JSON has no `Metadata` key (it's why every bench run logs
   "Failed to retrieve valid MediaContainer for first page of library N").

Only the NZB path (cli_mount layout, `<mount>/<category>/<job>`) is modelled so
far; debrid torrents and Zurg's flat `__all__` layout are not.

Scale: scenarios up to ~60 releases run in seconds. Past ~100 releases a run gets
slow in real time (a 120-release burst didn't finish 12 simulated minutes in 10
real minutes). Part of that is the app itself (see `test_checking_cost.py`), and
part is the bench capturing every DEBUG log line. Keep scenarios small, or profile
before adding a big one.
