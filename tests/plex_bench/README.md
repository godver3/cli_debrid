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

## Fidelity: checked against a real Plex

On 2026-11-01 the fake was compared with a throwaway Plex Media Server
(`plexinc/pms-docker`, new Plex agents, unclaimed, `ALLOWED_NETWORKS` for
token-less access) scanning short ffmpeg test clips. What the real server did, and
the fake now does:

1. Scans run one at a time on one thread. A refresh identical to one still waiting
   in the queue is merged (80 requests became 62 scans); a repeat of a folder that
   is already being scanned, or is done, runs again.
2. `?file=` is a case-insensitive substring of the part path (a basename, part of
   one, or the full path all match). In a show library it needs `type=4`, and
   without it Plex answers HTTP 500.
3. `guid=` on `/library/sections/N/all` matches only the item's own `plex://`
   guid. `imdb://`/`tmdb://` match nothing, also through plexapi's
   `section.search(guid=...)`, even though the item carries those Guid tags.
4. Section `recentlyAdded` lists new episodes as episodes (three new episodes of
   one season came back as three episodes, not the season).
5. An empty section's JSON has no `Metadata` key.
6. During a scan `/activities` has a `library.update.section` activity titled
   "Scanning <library>", and the section's `refreshing` attribute is true.
7. A refresh for a missing folder, or for a path outside the library's locations,
   still answers 200. The outside path is never scanned.
8. A file Plex can't match is still added, with a `local://` guid.

Not compared: scan speed on a FUSE mount (the fake's defaults are guesses), and
what Plex does with a file whose content is identical to one already indexed (it
treats it as the same file moved, so test clips must differ).

Only the NZB path (cli_mount layout, `<mount>/<category>/<job>`) is modelled so
far; debrid torrents and Zurg's flat `__all__` layout are not.

Scale: scenarios up to ~60 releases run in seconds. Past ~100 releases a run gets
slow in real time (a 120-release burst didn't finish 12 simulated minutes in 10
real minutes). Part of that is the app itself (see `test_checking_cost.py`), and
part is the bench capturing every DEBUG log line. Keep scenarios small, or profile
before adding a big one.
