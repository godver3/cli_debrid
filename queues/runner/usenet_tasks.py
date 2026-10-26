"""Usenet and cli_mount tasks: NZB health, playback repair, broken-item repair, mount sync.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
from datetime import datetime
import logging


class UsenetTasks:
    def task_nzb_health_check(self):
        """Poll NZB items in Adding state, run health checks, move to Checking or back to Wanted.
        Runs as a dedicated scheduled task so it doesn't block the Adding/Scraping queue threads.
        Guards: skips tick if previous run was slow; caps jobs per tick; per-job timeout."""
        from queues.run_program import ProgramRunner, _parse_last_updated  # defined there; imported when called
        import time as _time
        try:
            # Option 2: skip this tick if previous run took too long
            if self._nzb_health_last_duration >= self._NZB_HEALTH_SKIP_THRESHOLD:
                logging.debug(f'[NZBHealthCheck] Skipping tick — previous run took {self._nzb_health_last_duration:.1f}s')
                self._nzb_health_last_duration = 0.0  # reset so next tick runs
                return

            if not self.queue_manager:
                return
            adding_queue = self.queue_manager.queues.get('Adding')
            if not adding_queue:
                return

            _nzb_items = [
                item for item in adding_queue.items[:]
                if str(item.get('filled_by_torrent_id', '')).startswith('nzb:')
            ]
            if not _nzb_items:
                return

            _tick_start = _time.monotonic()

            from usenet import get_usenet_client
            from utilities.settings import get_setting as _gs
            from collections import defaultdict as _dd
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

            _nzb_client = get_usenet_client()

            # Group items by job_id
            _job_groups: dict = _dd(list)
            for _ni in _nzb_items:
                _jid = _ni.get('filled_by_torrent_id', '')[4:]
                _job_groups[_jid].append(_ni)

            # Download-timeout clock per job. It used to be each row's last_updated,
            # which every row write moves (sibling pulls, score updates...), so a
            # stuck job could outlive the timeout. Seed once from the oldest row so
            # the clock still survives restarts, then keep it until the job is gone.
            for _jid, _rows in _job_groups.items():
                if _jid not in self._nzb_job_started_at:
                    _seeds = [_parse_last_updated(_r.get('last_updated')) for _r in _rows]
                    _seeds = [_sd for _sd in _seeds if _sd is not None]
                    self._nzb_job_started_at[_jid] = min(_seeds) if _seeds else datetime.now()
            for _jid in [j for j in self._nzb_job_started_at if j not in _job_groups]:
                self._nzb_job_started_at.pop(_jid, None)

            # Option 3: cap jobs per tick. Rotate through the jobs: always taking the
            # first N left every job past the cap unpolled while the first N waited
            # (MaddGuru: 73 rows sat in Adding for hours).
            _all_jobs = list(_job_groups.keys())
            _cap = self._NZB_HEALTH_MAX_JOBS_PER_TICK
            if len(_all_jobs) > _cap:
                _start = ProgramRunner._nzb_health_job_cursor % len(_all_jobs)
                _unique_jobs = (_all_jobs[_start:] + _all_jobs[:_start])[:_cap]
                ProgramRunner._nzb_health_job_cursor = _start + _cap
                logging.debug(f'[NZBHealthCheck] Capping to {_cap}/{len(_all_jobs)} jobs this tick (from #{_start})')
            else:
                _unique_jobs = _all_jobs

            # Shared folder-wait counter dict (closure captures this)
            _job_folder_wait_counts = self._nzb_folder_wait_counts
            _folder_wait_max = self._NZB_FOLDER_WAIT_MAX

            def _poll_job(job_id):
                try:
                    status = _nzb_client.get_job_status(job_id)
                    if not status:
                        return job_id, None, None, None, None
                    raw = status.get('raw', {})
                    # Both clients return status['progress'] as a percentage. raw is the
                    # provider's own slot: cli_mount's 'progress' is 0-1, but a SAB slot
                    # (NzbDAV/Zurg) has none, and scaling the percentage again made any
                    # job at 1%+ look complete and wait for a folder with no timeout.
                    progress = int(float(status.get('progress', 0) or 0))
                    is_complete = raw.get('is_complete', False)
                    state = str(raw.get('state', status.get('state', ''))).lower()
                    inner_status = str(raw.get('status', '')).lower()
                    if is_complete or (inner_status == 'downloaded' and state in ('pausedup', 'completed', 'seeding')):
                        progress = 100
                    elif state in ('error', 'failed', 'bad') or raw.get('bad'):
                        progress = -1
                    elif status.get('state') == 'completed':
                        progress = 100
                    entry_name = None
                    actual_file_size = None
                    if progress == 100:
                        # If raw is empty the job isn't in cli_mount's queue.
                        # Wait a few ticks before declaring ghost — a brand-new submission
                        # may not appear in the queue API immediately.
                        # If the job was previously confirmed complete+folder found, cli_mount
                        # removed it after completion — treat as progress=100 not a ghost.
                        if not status.get('raw'):
                            if job_id in self._nzb_confirmed_complete:
                                logging.debug(f'[NZB] job {job_id} removed from cli_mount after completion — proceeding to Checking')
                                entry_name = self._nzb_confirmed_complete[job_id]
                                progress = 100
                            else:
                                _ghost_wait = _job_folder_wait_counts.get(job_id, 0) + 1
                                _job_folder_wait_counts[job_id] = _ghost_wait
                                if _ghost_wait >= 3:
                                    logging.warning(f'[NZB] job {job_id} not in cli_mount queue after {_ghost_wait} ticks — treating as ghost')
                                    _job_folder_wait_counts.pop(job_id, None)
                                    progress = -2
                                else:
                                    logging.debug(f'[NZB] job {job_id} not in queue yet (tick {_ghost_wait}/3) — waiting')
                                    progress = 99
                        if progress == 100:
                            items_for_job = _job_groups[job_id]
                            nzb_title = items_for_job[0].get('filled_by_file', '') or items_for_job[0].get('original_scraped_torrent_title', '')
                            search_name = nzb_title or job_id
                            try:
                                result = _nzb_client.get_nzb_file_info(
                                    search_name, fast_check=True, include_size=True
                                )
                                if result:
                                    entry_name, _, actual_file_size = result
                            except Exception:
                                pass
                        if not entry_name and progress == 100:
                            # Job completed (exists in queue) but folder not yet visible — wait up to max ticks.
                            wait_count = _job_folder_wait_counts.get(job_id, 0) + 1
                            _job_folder_wait_counts[job_id] = wait_count
                            if wait_count >= _folder_wait_max:
                                logging.warning(f'[NZB] job {job_id} folder never appeared after {wait_count} ticks — treating as broken')
                                _job_folder_wait_counts.pop(job_id, None)
                                progress = -1
                            else:
                                logging.debug(f'[NZB] job {job_id} complete but folder not found (tick {wait_count}/{_folder_wait_max}) — waiting')
                                progress = 99
                        elif progress == 100:
                            _job_folder_wait_counts.pop(job_id, None)
                    return job_id, progress, entry_name, actual_file_size, None
                except Exception as exc:
                    return job_id, None, None, None, exc

            # Option 3: cap workers at 5, per-job timeout
            _job_results = {}
            with _TPE(max_workers=min(len(_unique_jobs), 5)) as _pool:
                _futures = {_pool.submit(_poll_job, jid): jid for jid in _unique_jobs}
                try:
                    for _fut in _ac(_futures, timeout=self._NZB_HEALTH_JOB_TIMEOUT * len(_unique_jobs)):
                        try:
                            jid, prog, entry, actual_size, err = _fut.result(timeout=self._NZB_HEALTH_JOB_TIMEOUT)
                            _job_results[jid] = (prog, entry, actual_size, err)
                        except Exception:
                            pass
                except Exception:
                    # Timeout — process whatever completed so far, skip the rest
                    for _fut, jid in _futures.items():
                        if _fut.done():
                            try:
                                _, prog, entry, actual_size, err = _fut.result()
                                _job_results[jid] = (prog, entry, actual_size, err)
                            except Exception:
                                pass

            # Mark all completed entries as ready to move to Checking.
            # cli_mount's repair/health endpoint can stay in "repairing" indefinitely
            # and blocks items from reaching Checking. Since the folder is confirmed
            # present (entry_name is set), proceed immediately without waiting.
            _health_results = {}
            for jid, (prog, entry, actual_size, err) in _job_results.items():
                if prog == 100 and entry:
                    _health_results[entry] = None  # None = inconclusive, proceed to Checking
                    self._nzb_confirmed_complete[jid] = entry  # remember this job was complete+folder found
                    adding_queue._nzb_downloading_job_ids.discard(jid)  # no longer downloading

            # Process results per item
            _ghost_job_ids = [jid for jid, (prog, _, _, _) in _job_results.items() if prog == -2]
            if _ghost_job_ids:
                logging.warning(f'[NZB] Ghost job IDs detected in results: {_ghost_job_ids}')
            _moved_as_sibling = set()  # item IDs already batch-moved with their initiator
            for item in _nzb_items:
                torrent_id = item.get('filled_by_torrent_id', '')
                item_id = item.get('id')
                if item_id in _moved_as_sibling:
                    continue
                nzb_title = item.get('filled_by_file', '') or item.get('original_scraped_torrent_title', '')
                try:
                    # If this episode+version is already Collected under a different DB entry, remove
                    # this stale Adding entry rather than processing it.
                    if item.get('type') == 'episode' and item.get('episode_number') is not None:
                        try:
                            import re as _re_stale
                            from database.core import get_db_connection as _get_dbc_stale
                            from debrid.common import is_likely_season_pack as _is_pack_stale
                            _item_file = item.get('filled_by_file') or item.get('original_scraped_torrent_title') or ''
                            # Also checks filled_by_title - a provider can obfuscate the
                            # filename while the episode marker survives only there.
                            _is_individual = not _is_pack_stale(_item_file, item.get('filled_by_title') or '')
                            with _get_dbc_stale() as _sc:
                                if _is_individual:
                                    # Individual episode — match on same file under different entry
                                    _already_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                        "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                        "AND (filled_by_file=? OR original_scraped_torrent_title=?)",
                                        (item.get('imdb_id'), item.get('season_number'),
                                         item.get('episode_number'), item.get('version'), item_id,
                                         _item_file, _item_file)
                                    ).fetchone()[0]
                                else:
                                    # Season pack — check if THIS item was previously Collected
                                    # (has collected_at set) but got reset to Adding by sibling pull.
                                    # Also check for duplicate Collected entry with same pack source.
                                    _pack_match = _re_stale.search(r'\(([^)]+)\)\s*$', _item_file)
                                    _pack_orig = _pack_match.group(1) if _pack_match else ''
                                    # Check 1: same item previously collected
                                    _self_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items WHERE id=? AND collected_at IS NOT NULL",
                                        (item_id,)
                                    ).fetchone()[0]
                                    # Check 2: different entry with same pack source
                                    _other_collected = _sc.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                        "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                        "AND original_scraped_torrent_title=?",
                                        (item.get('imdb_id'), item.get('season_number'),
                                         item.get('episode_number'), item.get('version'), item_id,
                                         _pack_orig)
                                    ).fetchone()[0] if _pack_orig else 0
                                    _already_collected = _self_collected or _other_collected
                            if _already_collected:
                                logging.info(f'[NZB] Item {item_id} already Collected (self_collected={_self_collected if not _is_individual else "n/a"}) — removing stale Adding entry')
                                from database.database_writing import update_media_item_state as _umis_stale
                                _umis_stale(item_id, 'Collected')
                                adding_queue.remove_item(item)
                                continue
                        except Exception:
                            pass

                    job_id = torrent_id[4:]
                    if job_id not in _job_results:
                        # Job wasn't polled this tick (capped) — but still run stale check
                        if item.get('type') == 'episode' and item.get('episode_number') is not None:
                            try:
                                import re as _re_stale2
                                from database.core import get_db_connection as _get_dbc_stale2
                                from debrid.common import is_likely_season_pack as _is_pack_stale2
                                _item_file2 = item.get('filled_by_file') or item.get('original_scraped_torrent_title') or ''
                                _is_individual2 = not _is_pack_stale2(_item_file2, item.get('filled_by_title') or '')
                                with _get_dbc_stale2() as _sc2:
                                    if _is_individual2:
                                        _stale2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                            "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                            "AND (filled_by_file=? OR original_scraped_torrent_title=?)",
                                            (item.get('imdb_id'), item.get('season_number'),
                                             item.get('episode_number'), item.get('version'), item_id,
                                             _item_file2, _item_file2)
                                        ).fetchone()[0]
                                    else:
                                        _pack_m2 = _re_stale2.search(r'\(([^)]+)\)\s*$', _item_file2)
                                        _pack_o2 = _pack_m2.group(1) if _pack_m2 else ''
                                        _self2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items WHERE id=? AND collected_at IS NOT NULL",
                                            (item_id,)
                                        ).fetchone()[0]
                                        _other2 = _sc2.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE imdb_id=? AND season_number=? AND episode_number=? "
                                            "AND version=? AND type='episode' AND state='Collected' AND id!=? "
                                            "AND original_scraped_torrent_title=?",
                                            (item.get('imdb_id'), item.get('season_number'),
                                             item.get('episode_number'), item.get('version'), item_id, _pack_o2)
                                        ).fetchone()[0] if _pack_o2 else 0
                                        _stale2 = _self2 or _other2
                                if _stale2:
                                    logging.info(f'[NZB] Item {item_id} already Collected — removing stale Adding entry')
                                    from database.database_writing import update_media_item_state as _umis2
                                    _umis2(item_id, 'Collected')
                                    adding_queue.remove_item(item)
                                    continue
                            except Exception:
                                pass
                        continue
                    _prog, _entry, _actual_file_size, _err = _job_results[job_id]
                    if _err:
                        logging.debug(f'[NZB] Error polling progress for {torrent_id}: {_err}')
                        continue
                    progress = _prog
                    entry_name = _entry
                    if progress is None:
                        logging.debug(f'[NZB] {torrent_id} still queued/unknown — waiting')
                        continue
                    elif progress == -2:
                        try:
                            from database.nzb_playback_repair import reject_active_candidate
                            if reject_active_candidate(item_id, job_id, 'provider_job_missing'):
                                logging.warning(
                                    '[NZBPlayback] Provisional candidate %s disappeared; original retained and candidate excluded',
                                    job_id,
                                )
                                adding_queue.remove_item(item)
                                continue
                        except Exception as _playback_ghost_err:
                            logging.error('[NZBPlayback] Could not persist missing candidate %s: %s',
                                          job_id, _playback_ghost_err, exc_info=True)
                            continue
                        # Ghost job — never existed in cli_mount or already purged.
                        # Move primary item AND all siblings to Wanted (not retry in Adding)
                        # so they re-scrape fresh. Do NOT retry from scrape_results here
                        # because the dedup check would just re-assign the same dead hash.
                        logging.warning(f'[NZB] {torrent_id} is a ghost job — moving all items with this job to Wanted')
                        try:
                            from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                            blacklist_and_cleanup_nzb_failure(
                                item,
                                'ghost job in cli_mount',
                                clear_filled_by=False,
                                set_rescrape_title=True,
                            )
                        except Exception:
                            pass
                        # Move all items sharing this ghost torrent_id to Wanted
                        _ghost_items = [
                            s for s in adding_queue.items[:]
                            if s.get('filled_by_torrent_id') == torrent_id
                        ]
                        logging.warning(f'[NZB] Ghost job {torrent_id}: found {len(_ghost_items)} item(s) to move to Wanted')
                        for _gi in _ghost_items:
                            try:
                                _gi_id = _gi['id']
                                if _gi is not item:
                                    try:
                                        from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                                        blacklist_and_cleanup_nzb_failure(
                                            _gi,
                                            'ghost job sibling cleanup',
                                            clear_filled_by=False,
                                            set_rescrape_title=True,
                                        )
                                    except Exception:
                                        pass
                                # Cap how many times the same item can be resurrected into a
                                # ghost job before we stop retrying and blacklist instead -
                                # without this, an item whose reused reference keeps ghosting
                                # (e.g. a dead cli_mount job resurrected by a watchlist re-add)
                                # loops Wanted->Scraping->Adding forever with no escalation.
                                _ghost_count = self._nzb_ghost_repeat_counts.get(_gi_id, 0) + 1
                                self._nzb_ghost_repeat_counts[_gi_id] = _ghost_count
                                if _ghost_count >= self._NZB_GHOST_BLACKLIST_THRESHOLD:
                                    logging.warning(f'[NZB] Item {_gi_id} ghosted {_ghost_count}x — blacklisting instead of retrying again')
                                    self.queue_manager.move_to_blacklisted(_gi, 'Adding')
                                    self._nzb_ghost_repeat_counts.pop(_gi_id, None)
                                else:
                                    self.queue_manager.move_to_wanted(_gi, 'Adding')
                                    logging.info(f'[NZB] Moved ghost job item {_gi_id} to Wanted (ghost count {_ghost_count}/{self._NZB_GHOST_BLACKLIST_THRESHOLD})')
                            except Exception as _gi_err:
                                logging.error(f'[NZB] Failed to move ghost item {_gi.get("id")} to Wanted: {_gi_err}')
                        continue
                    elif progress == -1:
                        try:
                            from database.nzb_playback_repair import reject_active_candidate
                            if reject_active_candidate(item_id, job_id, 'provider_job_failed'):
                                logging.warning(
                                    '[NZBPlayback] Provisional candidate %s failed; original retained and candidate excluded',
                                    job_id,
                                )
                                adding_queue.remove_item(item)
                                continue
                        except Exception as _playback_fail_err:
                            logging.error('[NZBPlayback] Could not persist failed candidate %s: %s',
                                          job_id, _playback_fail_err, exc_info=True)
                            continue
                        logging.warning(f'[NZB] {torrent_id} failed in cli_mount — adding to not-wanted and moving back to Scraping')
                        try:
                            from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                            blacklist_and_cleanup_nzb_failure(
                                item,
                                'failed in cli_mount',
                                clear_filled_by=False,
                                set_rescrape_title=True,
                            )
                        except Exception as _nw_err:
                            logging.debug(f'[NZB] Could not run failure cleanup: {_nw_err}')
                        # Clean up all siblings sharing this dead job — add their URLs to not-wanted
                        # and move them back to Wanted so they re-scrape fresh without the dead job
                        try:
                            _dead_siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id and s.get('id') != item_id
                            ]
                            for _ds in _dead_siblings:
                                try:
                                    _ds_id = _ds['id']
                                    try:
                                        from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                                        blacklist_and_cleanup_nzb_failure(
                                            _ds,
                                            'failed-in-cli_mount sibling cleanup',
                                            clear_filled_by=False,
                                            set_rescrape_title=True,
                                        )
                                    except Exception:
                                        pass
                                    # Cap repeats for coalesced siblings the same way the primary
                                    # item below is capped. Without this, a sibling sharing the
                                    # dead job reference (e.g. a coalesced season pack) keeps
                                    # getting reset to Wanted every cycle with nothing ever
                                    # incrementing for it, reproducing the exact unbounded
                                    # Wanted->Scraping->Adding loop this cap exists to close -
                                    # just for the sibling instead of the primary item.
                                    _ds_fail_count = self._nzb_ghost_repeat_counts.get(_ds_id, 0) + 1
                                    self._nzb_ghost_repeat_counts[_ds_id] = _ds_fail_count
                                    if _ds_fail_count >= self._NZB_GHOST_BLACKLIST_THRESHOLD:
                                        logging.warning(f'[NZB] Sibling {_ds_id} failed-in-cli_mount {_ds_fail_count}x — blacklisting instead of retrying again')
                                        self._nzb_ghost_repeat_counts.pop(_ds_id, None)
                                        self.queue_manager.move_to_blacklisted(_ds, 'Adding')
                                    else:
                                        self.queue_manager.move_to_wanted(_ds, 'Adding')
                                        logging.info(f'[NZB] Cleaned up dead sibling {_ds_id} from Adding (failure count {_ds_fail_count}/{self._NZB_GHOST_BLACKLIST_THRESHOLD})')
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # Try next result from scrape_results directly in Adding (debrid-style flow).
                        # Clear torrent_id so the Adding queue process() loop picks this item up
                        # and tries the next available result — no health check cycle, no Scraping round-trip.
                        _has_more_results = False
                        try:
                            import json as _json
                            _sr = item.get('scrape_results', [])
                            if isinstance(_sr, str):
                                _sr = _json.loads(_sr)
                            # Filter out the bad URL from remaining results
                            _remaining = [r for r in (_sr or [])
                                          if (r.get('nzb_url') or r.get('magnet', '')) != item.get('filled_by_magnet', '')]
                            if _remaining:
                                _has_more_results = True
                                # Delete the broken job from cli_mount so prefix-match in
                                # torrent_processor won't reuse it on the next retry attempt.
                                # Safety: never delete if another live item (e.g. a different
                                # version sharing this job id due to a since-fixed dedup bug)
                                # still references it — that would delete its file too.
                                try:
                                    from usenet.repair_engine import _delete_from_provider as _dfp
                                    from database import get_db_connection as _gdb_stuck
                                    _broken_hash = torrent_id.replace('nzb:', '') if torrent_id else ''
                                    _conn_stuck = _gdb_stuck()
                                    try:
                                        _stuck_sibs = _conn_stuck.execute(
                                            "SELECT COUNT(*) FROM media_items "
                                            "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                            (torrent_id, item_id)
                                        ).fetchone()[0]
                                    finally:
                                        _conn_stuck.close()
                                    if _stuck_sibs:
                                        logging.info(f'[NZB] Skipping provider delete of {_broken_hash} — {_stuck_sibs} sibling(s) still active')
                                    else:
                                        _dfp(_broken_hash, item.get('filled_by_file', '') or '')
                                        logging.debug(f'[NZB] Deleted broken job {_broken_hash} from provider before retry')
                                except Exception as _del_err:
                                    logging.debug(f'[NZB] Could not delete broken job from provider: {_del_err}')
                                from database.database_writing import update_media_item as _umi_retry
                                _umi_retry(item_id,
                                    filled_by_torrent_id=None,
                                    filled_by_file=None,
                                    filled_by_title=None,
                                    filled_by_magnet=None,
                                    scrape_results=_json.dumps(_remaining),
                                )
                                item['filled_by_torrent_id'] = None
                                item['filled_by_magnet'] = None
                                adding_queue._nzb_submitted_ids.discard(item_id)
                                adding_queue._nzb_downloading_job_ids.discard(torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id)
                                logging.info(f'[NZB] {torrent_id} failed — {len(_remaining)} results remain, retrying in Adding')
                        except Exception as _retry_err:
                            logging.debug(f'[NZB] Could not prepare retry: {_retry_err}')
                        if not _has_more_results:
                            # Move back to Wanted for fresh re-scrape rather than blacklisting.
                            # NZB failures exhaust scrape_results quickly (not-wanted list filters them),
                            # but new results may be available on indexers — don't blacklist prematurely.
                            #
                            # That assumption only holds if the fresh re-scrape actually reaches a
                            # different NZB each time. If something keeps reusing the same dead
                            # reference (the not-wanted guid it just added above doesn't stop a
                            # title-match reuse elsewhere from grabbing it again), this becomes an
                            # unbounded loop instead of the intended "try something new" retry - so
                            # cap repeats of the identical outcome for this item before giving up.
                            _fail_count = self._nzb_ghost_repeat_counts.get(item_id, 0) + 1
                            self._nzb_ghost_repeat_counts[item_id] = _fail_count
                            try:
                                from database.database_writing import update_media_item as _umi_nw
                                _umi_nw(item_id, filled_by_torrent_id=None, filled_by_file=None,
                                        filled_by_title=None, filled_by_magnet=None,
                                        scrape_results=None, fall_back_to_single_scraper=False)
                            except Exception:
                                pass
                            if _fail_count >= self._NZB_GHOST_BLACKLIST_THRESHOLD:
                                logging.warning(f'[NZB] Item {item_id} failed-in-cli_mount {_fail_count}x with no new results each time — blacklisting instead of retrying again')
                                self._nzb_ghost_repeat_counts.pop(item_id, None)
                                self.queue_manager.move_to_blacklisted(item, 'Adding')
                            else:
                                logging.info(f'[NZB] {torrent_id} no remaining results — moving to Wanted for fresh scrape (failure count {_fail_count}/{self._NZB_GHOST_BLACKLIST_THRESHOLD})')
                                self.queue_manager.move_to_wanted(item, 'Adding')
                        continue
                    elif progress < 100:
                        # Check if this job has been downloading too long without finishing.
                        # Uses the job's own clock (see _nzb_job_started_at), which row
                        # writes don't reset and restarts don't lose. Default cap: 2 hours.
                        _download_timeout_hours = float(_gs('Usenet Provider', 'nzb_download_timeout_hours', 2.0))
                        _job_started = self._nzb_job_started_at.get(job_id)
                        _timed_out_dl = False
                        if _job_started and _download_timeout_hours > 0:
                            _age_hours = (datetime.now() - _job_started).total_seconds() / 3600.0
                            if _age_hours > _download_timeout_hours:
                                _timed_out_dl = True
                        if not _timed_out_dl:
                            # Mark job as actively downloading after 1 minute so Adding queue skips it
                            _job_id_dl = torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id
                            try:
                                from datetime import datetime as _dt_dl
                                _lu_dl = item.get('last_updated')
                                if _lu_dl:
                                    _lu_dl_dt = _dt_dl.fromisoformat(str(_lu_dl).replace('Z', '+00:00').split('+')[0])
                                    _age_mins = (_dt_dl.now() - _lu_dl_dt).total_seconds() / 60.0
                                    if _age_mins >= 1.0 and _job_id_dl not in adding_queue._nzb_downloading_job_ids:
                                        adding_queue._nzb_downloading_job_ids.add(_job_id_dl)
                                        logging.info(f'[NZB] {torrent_id} downloading ({progress}%) >1min — marked as Downloading, Adding queue will skip')
                            except Exception:
                                pass
                            logging.debug(f'[NZB] {torrent_id} progress={progress}% — waiting')
                            continue
                        try:
                            from database.nzb_playback_repair import reject_active_candidate
                            if reject_active_candidate(item_id, job_id, 'provider_job_timeout'):
                                logging.warning(
                                    '[NZBPlayback] Provisional candidate %s timed out; original retained and candidate excluded',
                                    job_id,
                                )
                                adding_queue.remove_item(item)
                                continue
                        except Exception as _playback_timeout_err:
                            logging.error('[NZBPlayback] Could not persist timed-out candidate %s: %s',
                                          job_id, _playback_timeout_err, exc_info=True)
                            continue
                        # Download timed out — treat like a failed job: add URL to not-wanted,
                        # delete from cli_mount, try next scrape result or move to Wanted.
                        logging.warning(
                            f'[NZB] {torrent_id} stalled at {progress}% after >{_download_timeout_hours:.1f}h — '
                            f'adding to not-wanted and requeuing'
                        )
                        try:
                            from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid_to
                            _nzb_url_to = item.get('filled_by_magnet', '')
                            if _nzb_url_to:
                                _add_guid_to(_nzb_url_to)
                                logging.info(f'[NZB] Added stalled URL to not-wanted')
                        except Exception:
                            pass
                        # Delete the stalled job from cli_mount
                        try:
                            import requests as _req_to
                            _dcy_url_to = _gs('Usenet Provider', 'url', default='').rstrip('/')
                            _dcy_token_to = _gs('Usenet Provider', 'api_token', default='')
                            _headers_to = {'Authorization': f'Bearer {_dcy_token_to}'} if _dcy_token_to else {}
                            _search_to = (entry_name or nzb_title or '').strip()
                            _real_hash_to = None
                            _page_to = 1
                            while not _real_hash_to:
                                _tr_to = _req_to.get(f'{_dcy_url_to}/api/torrents',
                                                     params={'page': _page_to, 'limit': 50, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                                     headers=_headers_to, timeout=10)
                                if _tr_to.status_code != 200:
                                    break
                                _data_to = _tr_to.json()
                                for _t_to in _data_to.get('torrents', []):
                                    if _t_to.get('name', '').strip() == _search_to:
                                        _real_hash_to = _t_to.get('info_hash', '')
                                        break
                                if _real_hash_to or not _data_to.get('has_next'):
                                    break
                                _page_to += 1
                            if _real_hash_to:
                                # Safety: never delete if another live item (e.g. a different
                                # version sharing this job id due to a since-fixed dedup bug)
                                # still references it — that would delete its file too.
                                from database import get_db_connection as _gdb_stall
                                _conn_stall = _gdb_stall()
                                try:
                                    _stall_sibs = _conn_stall.execute(
                                        "SELECT COUNT(*) FROM media_items "
                                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                                        (torrent_id, item_id)
                                    ).fetchone()[0]
                                finally:
                                    _conn_stall.close()
                                if _stall_sibs:
                                    logging.info(f'[NZB] Skipping provider delete of stalled job {_real_hash_to} — {_stall_sibs} sibling(s) still active')
                                else:
                                    _dr_to = _req_to.delete(f'{_dcy_url_to}/api/torrents',
                                                            headers=_headers_to,
                                                            params={'hashes': _real_hash_to}, timeout=10)
                                    if _dr_to.status_code == 200:
                                        logging.info(f'[NZB] Deleted stalled job {_real_hash_to} from cli_mount')
                                    else:
                                        logging.warning(f'[NZB] Could not delete stalled job: HTTP {_dr_to.status_code}')
                        except Exception as _del_to_err:
                            logging.debug(f'[NZB] Could not delete stalled job from cli_mount: {_del_to_err}')
                        # Handle siblings sharing this stalled job
                        try:
                            _stalled_siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id and s.get('id') != item_id
                            ]
                            for _ss in _stalled_siblings:
                                _ss_url = _ss.get('filled_by_magnet', '')
                                if _ss_url:
                                    try:
                                        _add_guid_to(_ss_url)
                                    except Exception:
                                        pass
                                self.queue_manager.move_to_wanted(_ss, 'Adding')
                                logging.info(f'[NZB] Moved stalled sibling {_ss["id"]} to Wanted')
                        except Exception:
                            pass
                        # Try next result from scrape_results (same as progress=-1 retry flow)
                        _has_more_to = False
                        try:
                            import json as _json_to
                            _sr_to = item.get('scrape_results', [])
                            if isinstance(_sr_to, str):
                                _sr_to = _json_to.loads(_sr_to)
                            _remaining_to = [r for r in (_sr_to or [])
                                             if (r.get('nzb_url') or r.get('magnet', '')) != item.get('filled_by_magnet', '')]
                            if _remaining_to:
                                _has_more_to = True
                                from database.database_writing import update_media_item as _umi_to
                                _umi_to(item_id,
                                    filled_by_torrent_id=None,
                                    filled_by_file=None,
                                    filled_by_title=None,
                                    filled_by_magnet=None,
                                    scrape_results=_json_to.dumps(_remaining_to),
                                )
                                item['filled_by_torrent_id'] = None
                                item['filled_by_magnet'] = None
                                adding_queue._nzb_submitted_ids.discard(item_id)
                                adding_queue._nzb_downloading_job_ids.discard(torrent_id[4:] if torrent_id.startswith('nzb:') else torrent_id)
                                logging.info(f'[NZB] Stalled job — {len(_remaining_to)} results remain, retrying in Adding')
                        except Exception as _retry_to_err:
                            logging.debug(f'[NZB] Could not prepare stall retry: {_retry_to_err}')
                        if not _has_more_to:
                            logging.info(f'[NZB] {torrent_id} stalled, no remaining results — moving to Wanted for fresh scrape')
                            try:
                                from database.database_writing import update_media_item as _umi_st
                                _umi_st(item_id, filled_by_torrent_id=None, filled_by_file=None,
                                        filled_by_title=None, filled_by_magnet=None,
                                        scrape_results=None, fall_back_to_single_scraper=False)
                            except Exception:
                                pass
                            self.queue_manager.move_to_wanted(item, 'Adding')
                        continue

                    logging.info(f'[NZB] {torrent_id} complete — running health check')

                    # If this is a season pack initiator, wait until all siblings have
                    # coalesced into Adding before moving to Checking. This ensures
                    # _resolve_nzb_file_info sees all episodes as a pack and assigns
                    # correct per-episode filenames, preventing Plex duplicate rows.
                    _imdb_wait = item.get('imdb_id')
                    _season_wait = item.get('season_number')
                    # Only wait for siblings when this is a season pack NZB.
                    # Individual episode NZBs (title contains SxxExx) are self-contained —
                    # their sibling episodes each have their own job and don't need to
                    # coalesce before _resolve_nzb_file_info runs.
                    from debrid.common import is_likely_season_pack as _is_pack_ep
                    _is_individual_ep = not _is_pack_ep(nzb_title, item.get('filled_by_title') or '')
                    if _imdb_wait and _season_wait is not None and not _is_individual_ep:
                        try:
                            from database.core import get_db_connection as _get_dbc
                            with _get_dbc() as _wconn:
                                # Only count Scraping siblings that are recently updated (within 10 min).
                                # A sibling stuck in Scraping with no results will have a stale
                                # last_updated and should not block the pack indefinitely.
                                _scraping_siblings = _wconn.execute(
                                    "SELECT COUNT(*) FROM media_items "
                                    "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                    "AND state='Scraping' AND id!=? "
                                    "AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id!=?) "
                                    "AND last_updated >= datetime('now', '-10 minutes')",
                                    (_imdb_wait, _season_wait, item_id, torrent_id)
                                ).fetchone()[0]
                            if _scraping_siblings > 0:
                                logging.debug(
                                    f'[NZB] {torrent_id} complete but {_scraping_siblings} siblings still Scraping — waiting one tick'
                                )
                                # While waiting for Scraping siblings, pull any Wanted/Sleeping
                                # siblings into Adding now so they don't wait behind the throttle.
                                from debrid.common import is_likely_season_pack as _is_pack_early_fn
                                _is_pack_early = _is_pack_early_fn(nzb_title, item.get('filled_by_title') or '')
                                if _is_pack_early:
                                    try:
                                        from database.core import get_db_connection as _get_dbc_e
                                        from database.database_writing import update_media_item as _umi_e
                                        from database.database_writing import update_media_item_state as _umis_e
                                        _nzb_url_e = item.get('filled_by_magnet', '') or item.get('link', '')
                                        _orig_e = item.get('original_scraped_torrent_title', nzb_title)
                                        with _get_dbc_e() as _ec:
                                            _wanted_e = _ec.execute(
                                                "SELECT id FROM media_items "
                                                "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                                "AND (state IN ('Wanted','Sleeping') OR (state='Adding' AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id=''))) AND id!=? "
                                                "AND collected_at IS NULL "
                                                "AND episode_number NOT IN ("
                                                "  SELECT episode_number FROM media_items "
                                                "  WHERE imdb_id=? AND season_number=? "
                                                "  AND type='episode' AND state='Collected'"
                                                ")",
                                                (_imdb_wait, _season_wait, item_id,
                                                 _imdb_wait, _season_wait)
                                            ).fetchall()
                                        _folder_e = entry_name or nzb_title
                                        _seg_e = item.get('nzb_segment_id', '') or ''
                                        for _we in _wanted_e:
                                            try:
                                                _seg_e_kw = {'nzb_segment_id': _seg_e} if _seg_e else {}
                                                # Same release as the pack, so same score/resolution.
                                                _score_e_kw = {k: item.get(k) for k in ('current_score', 'resolution') if item.get(k)}
                                                _umi_e(_we[0], filled_by_torrent_id=torrent_id,
                                                       filled_by_file=_folder_e,
                                                       filled_by_magnet=_nzb_url_e,
                                                       filled_by_title=nzb_title,
                                                       original_scraped_torrent_title=_orig_e,
                                                       **_seg_e_kw, **_score_e_kw)
                                                _umis_e(_we[0], 'Adding')
                                                logging.info(f'[NZB] Pulled Wanted sibling {_we[0]} into Adding (pack waiting for Scraping)')
                                            except Exception:
                                                pass
                                        if _wanted_e:
                                            adding_queue.update()
                                    except Exception:
                                        pass
                                continue
                        except Exception:
                            pass

                    if not entry_name:
                        # Should not happen — poll_job now keeps progress at 99 until folder found.
                        # Guard: stay in Adding rather than moving to Checking with no folder.
                        logging.warning(f'[NZB] {torrent_id} has no entry_name at health check — waiting for folder')
                        continue
                    else:
                        health = _health_results.get(entry_name)

                    # A completed individual NZB whose selected video is only a
                    # tiny fraction of the indexer's advertised release size is
                    # almost certainly a trailer, sample, or incomplete payload.
                    # Reuse the existing broken-job cleanup/blacklist/retry flow.
                    if health != 'broken':
                        try:
                            from usenet.nzb_size_validation import individual_nzb_size_mismatch
                            _size_mismatch = individual_nzb_size_mismatch(
                                item, nzb_title, _actual_file_size
                            )
                            if _size_mismatch:
                                _actual_b, _expected_b, _ratio = _size_mismatch
                                logging.warning(
                                    f'[NZB] {torrent_id} actual video is only {_ratio:.1%} of '
                                    f'advertised size ({_actual_b / (1024 ** 2):.1f} MiB vs '
                                    f'{_expected_b / (1024 ** 3):.2f} GiB) — treating as broken'
                                )
                                health = 'broken'
                        except Exception as _size_err:
                            logging.debug(f'[NZB] Could not validate completed file size: {_size_err}')

                    if health == 'broken':
                        logging.warning(f'[NZB] {torrent_id} entry {entry_name!r} is BROKEN — deleting and moving back to Wanted')
                        if entry_name:
                            _health_results.pop(entry_name, None)
                        self._nzb_health_triggered.pop(entry_name, None)
                        try:
                            import requests as _req
                            _dcy_url = _gs('Usenet Provider', 'url', default='').rstrip('/')
                            _dcy_token = _gs('Usenet Provider', 'api_token', default='')
                            _headers = {'Authorization': f'Bearer {_dcy_token}'} if _dcy_token else {}
                            _real_hash = None
                            _search_name = (entry_name or nzb_title or '').strip()
                            _page = 1
                            while not _real_hash:
                                _tr = _req.get(f'{_dcy_url}/api/torrents',
                                               params={'page': _page, 'limit': 50, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                               headers=_headers, timeout=10)
                                if _tr.status_code != 200:
                                    break
                                _data = _tr.json()
                                for _t in _data.get('torrents', []):
                                    if _t.get('name', '').strip() == _search_name:
                                        _real_hash = _t.get('info_hash', '')
                                        break
                                if _real_hash or not _data.get('has_next'):
                                    break
                                _page += 1
                            if _real_hash:
                                _dr = _req.delete(f'{_dcy_url}/api/torrents', headers=_headers,
                                                  params={'hashes': _real_hash}, timeout=10)
                                if _dr.status_code == 200:
                                    logging.info(f'[NZB] Deleted broken entry {_real_hash} from cli_mount')
                                else:
                                    logging.warning(f'[NZB] Could not delete {_real_hash}: HTTP {_dr.status_code}')
                            else:
                                logging.warning(f'[NZB] Entry {_search_name!r} not found in /api/torrents')
                        except Exception as _de:
                            logging.warning(f'[NZB] Error deleting broken entry: {_de}')
                        try:
                            from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                            blacklist_and_cleanup_nzb_failure(
                                item,
                                'health check broken',
                                clear_filled_by=True,
                                set_rescrape_title=True,
                                nzb_title=nzb_title,
                                fetch_segment_if_missing=True,
                            )
                        except Exception as _nwe:
                            logging.debug(f'[NZB] Could not run broken-job cleanup: {_nwe}')
                        try:
                            import json as _json
                            from database.database_writing import update_media_item as _umi
                            _results = item.get('scrape_results', [])
                            if isinstance(_results, str):
                                _results = _json.loads(_results)
                            _filtered = [r for r in (_results or []) if r.get('title', '') != nzb_title and r.get('original_title', '') != nzb_title]
                            if _filtered and len(_filtered) < len(_results):
                                _umi(item_id, scrape_results=_json.dumps(_filtered), fall_back_to_single_scraper=False)
                                logging.info(f'[NZB] Removed broken result, {len(_filtered)} remaining — retrying Adding')
                                item['scrape_results'] = _json.dumps(_filtered)
                            else:
                                logging.info(f'[NZB] No remaining scrape results — moving back to Wanted')
                                self.queue_manager.move_to_wanted(item, 'Adding')
                        except Exception as _re:
                            logging.warning(f'[NZB] Could not remove broken result: {_re}')
                            self.queue_manager.move_to_wanted(item, 'Adding')

                        # Clean up all coalesced siblings sharing this broken job.
                        # They are stuck in Adding with the same filled_by_torrent_id.
                        # Add their NZB URL to not-wanted, reset their fields, move to Wanted.
                        _broken_siblings = [
                            s for s in adding_queue.items[:]
                            if s.get('filled_by_torrent_id') == torrent_id
                            and s.get('id') != item_id
                        ]
                        if _broken_siblings:
                            logging.warning(f'[NZB] Cleaning up {len(_broken_siblings)} coalesced siblings of broken job {torrent_id}')
                            from database.database_writing import update_media_item as _umi2
                            for _sib in _broken_siblings:
                                try:
                                    try:
                                        from utilities.nzb_failure_cleanup import blacklist_and_cleanup_nzb_failure
                                        blacklist_and_cleanup_nzb_failure(
                                            _sib,
                                            'health check broken sibling cleanup',
                                            clear_filled_by=False,
                                            set_rescrape_title=True,
                                            nzb_title=nzb_title,
                                        )
                                    except Exception:
                                        pass
                                    _umi2(_sib['id'],
                                          filled_by_torrent_id=None,
                                          filled_by_file=None,
                                          filled_by_title=None,
                                          filled_by_magnet=None,
                                          debrid_folder_name=None,
                                          fall_back_to_single_scraper=False)
                                    self.queue_manager.move_to_wanted(_sib, 'Adding')
                                    logging.info(f'[NZB] Moved broken sibling {_sib["id"]} back to Wanted')
                                except Exception as _sib_err:
                                    logging.warning(f'[NZB] Could not clean up sibling {_sib.get("id")}: {_sib_err}')
                        item['filled_by_torrent_id'] = None
                        item['filled_by_file'] = None
                        item['filled_by_title'] = None
                        item['debrid_folder_name'] = None
                        continue
                    else:
                        if health == 'healthy':
                            logging.info(f'[NZB] {torrent_id} health check passed — moving to Checking')
                        else:
                            logging.warning(f'[NZB] {torrent_id} health check inconclusive — proceeding to Checking anyway')
                        nzb_url = item.get('filled_by_magnet', '') or item.get('link', '')
                        nzb_original_title = item.get('original_scraped_torrent_title', nzb_title)

                        # Pull any Wanted/Sleeping siblings for this season directly into Adding
                        # bypassing the Scraping queue and throttle — the pack is already confirmed
                        # working so there's no need to scrape them individually.
                        _imdb_pull = item.get('imdb_id')
                        _season_pull = item.get('season_number')
                        from debrid.common import is_likely_season_pack as _is_pack_fn
                        _is_pack = _is_pack_fn(nzb_title, item.get('filled_by_title') or '')
                        if _imdb_pull and _season_pull is not None and _is_pack:
                            try:
                                from database.core import get_db_connection as _get_dbc2
                                from database.database_writing import update_media_item as _umi_pull
                                with _get_dbc2() as _pconn:
                                    _wanted_sibs = _pconn.execute(
                                        "SELECT id FROM media_items "
                                        "WHERE imdb_id=? AND season_number=? AND type='episode' "
                                        "AND (state IN ('Wanted','Sleeping') OR (state='Adding' AND (filled_by_torrent_id IS NULL OR filled_by_torrent_id=''))) AND id!=? "
                                        "AND collected_at IS NULL "
                                        "AND episode_number NOT IN ("
                                        "  SELECT episode_number FROM media_items "
                                        "  WHERE imdb_id=? AND season_number=? "
                                        "  AND type='episode' AND state='Collected'"
                                        ")",
                                        (_imdb_pull, _season_pull, item_id,
                                         _imdb_pull, _season_pull)
                                    ).fetchall()
                                _folder_pull = entry_name or nzb_title
                                _pull_seg_id = item.get('nzb_segment_id', '') or ''
                                for _ws in _wanted_sibs:
                                    try:
                                        _pull_seg_kwargs = {'nzb_segment_id': _pull_seg_id} if _pull_seg_id else {}
                                        # Same release as the pack, so same score/resolution.
                                        _pull_score_kwargs = {k: item.get(k) for k in ('current_score', 'resolution') if item.get(k)}
                                        _umi_pull(_ws[0],
                                            filled_by_torrent_id=torrent_id,
                                            filled_by_file=_folder_pull,
                                            filled_by_magnet=nzb_url,
                                            filled_by_title=nzb_title,
                                            original_scraped_torrent_title=nzb_original_title,
                                            **_pull_seg_kwargs,
                                            **_pull_score_kwargs,
                                        )
                                        from database.database_writing import update_media_item_state as _umis_pull
                                        _umis_pull(_ws[0], 'Adding')
                                        logging.info(f'[NZB] Pulled Wanted sibling {_ws[0]} directly into Adding for pack {torrent_id}')
                                    except Exception as _pull_err:
                                        logging.debug(f'[NZB] Could not pull sibling {_ws[0]}: {_pull_err}')
                                if _wanted_sibs:
                                    # Reload Adding queue so new siblings are visible this tick
                                    adding_queue.update()
                            except Exception as _pull_ex:
                                logging.debug(f'[NZB] Wanted sibling pull failed: {_pull_ex}')
                        try:
                            _folder_name = entry_name or nzb_title
                            self.queue_manager.move_to_checking(
                                item, 'Adding',
                                title=nzb_title,
                                link=nzb_url,
                                filled_by_file=_folder_name,
                                torrent_id=torrent_id,
                                debrid_folder_name=_folder_name,
                                original_scraped_torrent_title=nzb_original_title,
                            )
                            # Item cleared Adding on a real, live job — any prior ghost-job
                            # repeats for it are no longer relevant to a future streak.
                            self._nzb_ghost_repeat_counts.pop(item_id, None)
                            # Move all coalesced siblings (same torrent_id, still in Adding,
                            # no filled_by_file yet) to Checking together with the initiator.
                            # _resolve_nzb_file_info will assign per-episode filenames to all
                            # of them once they're in Checking.
                            _siblings = [
                                s for s in adding_queue.items[:]
                                if s.get('filled_by_torrent_id') == torrent_id
                                and s.get('id') != item_id
                                and s.get('filled_by_file') is None
                            ]
                            for _sib in _siblings:
                                try:
                                    self.queue_manager.move_to_checking(
                                        _sib, 'Adding',
                                        title=nzb_title,
                                        link=nzb_url,
                                        filled_by_file=None,  # _resolve_nzb_file_info will set per-episode filename
                                        torrent_id=torrent_id,
                                        debrid_folder_name=_folder_name,
                                        original_scraped_torrent_title=nzb_original_title,
                                    )
                                    _moved_as_sibling.add(_sib['id'])
                                    self._nzb_ghost_repeat_counts.pop(_sib['id'], None)
                                    logging.info(f'[NZB] Moved coalesced sibling {_sib["id"]} to Checking with initiator')
                                except Exception as _se:
                                    logging.warning(f'[NZB] Could not move sibling {_sib.get("id")} to Checking: {_se}')
                        except Exception as _ce:
                            logging.warning(f'[NZB] Could not move {torrent_id} to Checking: {_ce}')
                            adding_queue._handle_failed_item(item, f'NZB health passed but queue move failed: {_ce}', self.queue_manager)
                except Exception as _exc:
                    logging.error(f'[NZB] Error in health check for {torrent_id}: {_exc}', exc_info=True)

            self._nzb_health_last_duration = _time.monotonic() - _tick_start
            logging.debug(f'[NZBHealthCheck] Tick completed in {self._nzb_health_last_duration:.1f}s ({len(_unique_jobs)} jobs)')

            # --- ARTICLE_NOT_FOUND / broken item detection ---
            # After each health check tick, poll provider for broken items.
            # Rate-limited: only run every 5 minutes to avoid hammering the API.
            # Safety guard: skip items whose info_hash matches something currently
            # in Adding or Checking state — those are new submissions still in flight,
            # not genuinely broken collected items.
            try:
                _now_ts = _time.monotonic()
                if not hasattr(self, '_last_broken_check_ts'):
                    self._last_broken_check_ts = 0.0
                if _now_ts - self._last_broken_check_ts >= 300:  # 5 minutes
                    self._last_broken_check_ts = _now_ts
                    from usenet.repair_engine import fetch_broken_items, run_repair
                    _broken = fetch_broken_items()
                    if _broken:
                        # Build set of in-flight hashes (Adding + Checking) to exclude
                        try:
                            from database.core import get_db_connection as _get_dbc_hc
                            with _get_dbc_hc() as _hc_conn:
                                _inflight_rows = _hc_conn.execute(
                                    "SELECT filled_by_torrent_id FROM media_items "
                                    "WHERE state IN ('Adding','Checking') "
                                    "AND filled_by_torrent_id LIKE 'nzb:%'"
                                ).fetchall()
                            _inflight_hashes = {r[0][4:] for r in _inflight_rows if r[0]}
                        except Exception:
                            _inflight_hashes = set()
                        # Filter out in-flight items — they're not genuinely broken
                        _truly_broken = [
                            e for e in _broken
                            if (e.get('info_hash') or e.get('hash') or '') not in _inflight_hashes
                        ]
                        if _truly_broken:
                            logging.warning(
                                f'[NZBHealthCheck] Detected {len(_truly_broken)} broken item(s) '
                                f'(excluded {len(_broken)-len(_truly_broken)} in-flight) — triggering repair'
                            )
                            run_repair(triggered_by='auto_detected')
                        elif _broken:
                            logging.debug(
                                f'[NZBHealthCheck] {len(_broken)} broken item(s) all in-flight — skipping repair'
                            )
            except Exception as _broken_err:
                logging.debug(f'[NZBHealthCheck] Broken item check error: {_broken_err}')

            # --- cli_mount log watcher: ARTICLE_NOT_FOUND + cache warm failures ---
            # Tails cli_mount's log for two dead-file signals:
            #   1. [webdav] Error streaming file: ... ARTICLE_NOT_FOUND
            #   2. [manager] cache warm failed ... input/output error
            # Both mean the same thing: segments expired, file is unreadable.
            # Acting early (before Plex analyzes the file) prevents the Plex SQLite
            # WAL deadlock caused by --analyze processes stuck in kernel D-state.
            # Rate-limited to every 30s.
            try:
                _now_watcher = _time.monotonic()
                if not hasattr(self, '_last_webdav_check_ts'):
                    self._last_webdav_check_ts = 0.0
                    self._last_webdav_log_pos = -1  # -1 = uninitialized, seek to EOF on first run
                    self._webdav_repaired_entries = set()  # entries processed this session
                if _now_watcher - self._last_webdav_check_ts >= 30:
                    self._last_webdav_check_ts = _now_watcher
                    import re as _re_wv, os as _os_wv
                    from utilities.settings import get_setting as _gs_wv
                    _dcy_data = _gs_wv('Usenet Provider', 'data_path', '/climount_data').rstrip('/')
                    _dcy_log = f'{_dcy_data}/logs/climount.log'
                    if _os_wv.path.isfile(_dcy_log):
                        _fsize = _os_wv.path.getsize(_dcy_log)
                        # On first run, skip to end of log — don't reprocess old errors
                        if self._last_webdav_log_pos < 0:
                            self._last_webdav_log_pos = _fsize
                        # Reset position if log was rotated
                        if _fsize < self._last_webdav_log_pos:
                            self._last_webdav_log_pos = 0
                        if _fsize > self._last_webdav_log_pos:
                            with open(_dcy_log, 'r', errors='replace') as _lf:
                                _lf.seek(self._last_webdav_log_pos)
                                _new_lines = _lf.read()
                                self._last_webdav_log_pos = _lf.tell()
                            # Pattern 1a: DFS backend (Hanwen/native)
                            # [dfs] download error error="NNTP ARTICLE_NOT_FOUND..." entry="<entry_name>"
                            _dfs_pattern = _re_wv.compile(
                                r'\[dfs\] download error error="(?:.*?)ARTICLE_NOT_FOUND[^"]*" count=\d+ entry="([^"]+)"'
                            )
                            # Pattern 1b: rclone/WebDAV backend
                            # [webdav] Error streaming file: <entry_name>/<filename> error="NNTP ARTICLE_NOT_FOUND..."
                            _webdav_pattern = _re_wv.compile(
                                r'\[webdav\] Error streaming file: (.+?)/[^/]+ error="(?:.*?)ARTICLE_NOT_FOUND'
                            )
                            # Pattern 2: cache warm failures (file unreadable, I/O error)
                            # Format: [dfs] stream error ... entry="<entry>" filename="<file>"
                            # Also catches: [manager] cache warm failed error="read .../cli_debrid/<entry>/...: input/output error"
                            _cache_warm_pattern = _re_wv.compile(
                                r'(?:\[dfs\] stream error.*?entry="([^"]+)"|\[manager\] cache warm failed error="read [^/]+/[^/]+/([^/"]+)[^"]*: input/output error")'
                            )
                            _broken_entries = {}
                            for _m in _dfs_pattern.finditer(_new_lines):
                                _entry_name = _m.group(1).strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = True
                            for _m in _webdav_pattern.finditer(_new_lines):
                                _entry_name = _m.group(1).strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = True
                            for _m in _cache_warm_pattern.finditer(_new_lines):
                                # Two capture groups — one per sub-pattern alternative
                                _entry_name = (_m.group(1) or _m.group(2) or '').strip()
                                if _entry_name and _entry_name not in _broken_entries and _entry_name not in self._webdav_repaired_entries:
                                    _broken_entries[_entry_name] = 'cache_warm'

                            if _broken_entries:
                                _webdav_count = sum(1 for v in _broken_entries.values() if v is True)
                                _cache_count = sum(1 for v in _broken_entries.values() if v == 'cache_warm')
                                logging.warning(
                                    f'[NZBHealthCheck] Detected {len(_broken_entries)} dead entry(ies) '
                                    f'from cli_mount log ({_webdav_count} ARTICLE_NOT_FOUND, {_cache_count} cache warm I/O error) '
                                    f'— triggering immediate repair'
                                )
                                # Trigger repair immediately for these specific entries
                                try:
                                    from usenet.repair_engine import (
                                        _find_db_items_by_entry_name,
                                        _delete_from_provider,
                                        _move_to_wanted,
                                        _blacklist_broken_nzb,
                                        _resolve_info_hash_from_provider,
                                    )
                                    from database.nzb_repair_activity import log_repair_activity
                                    from usenet.repair_engine import _backfill_hash_for_item
                                    for _ename in _broken_entries:
                                        try:
                                            # Resolve info_hash for provider deletion
                                            _ihash = _resolve_info_hash_from_provider(_ename)
                                            # Find DB items
                                            from usenet.repair_engine import AMBIGUOUS as _AMBIGUOUS_WD
                                            _items = _find_db_items_by_entry_name(_ename)
                                            if _items is _AMBIGUOUS_WD:
                                                # Multiple versions ambiguously matched — do not delete
                                                # from provider, a live row we can't identify may need it.
                                                logging.warning(f'[NZBHealthCheck] {_ename!r} — ambiguous multi-version match, skipping without deleting')
                                                self._webdav_repaired_entries.add(_ename)
                                                continue
                                            _items = [i for i in _items
                                                      if i.get('state') in ('Collected', 'Checking', 'Upgrading')]
                                            if not _items:
                                                # Also try hash lookup
                                                if _ihash:
                                                    from usenet.repair_engine import _find_db_item_by_info_hash
                                                    _single = _find_db_item_by_info_hash(_ihash)
                                                    if _single and _single.get('state') in ('Collected', 'Checking', 'Upgrading'):
                                                        _items = [_single]
                                            # If hash still empty but we have DB items, run targeted backfill
                                            if not _ihash and _items:
                                                _ihash = _backfill_hash_for_item(_items[0])
                                                if _ihash:
                                                    logging.info(f'[NZBHealthCheck] Targeted backfill resolved hash for {_ename!r}: {_ihash!r}')
                                            if not _items:
                                                logging.debug(f'[NZBHealthCheck] No DB items for broken webdav entry {_ename!r} — deleting from provider only')
                                                _delete_from_provider(_ihash, _ename)
                                                self._webdav_repaired_entries.add(_ename)
                                                continue
                                            logging.warning(
                                                f'[NZBHealthCheck] Immediate repair: {_ename!r} — '
                                                f'{len(_items)} item(s), hash={_ihash!r}'
                                            )
                                            # Blacklist all unique NZB URLs + segment IDs across all items in this entry
                                            _seen_urls = set()
                                            for _bi in _items:
                                                _nzb_url = _bi.get('filled_by_magnet', '') or ''
                                                _seg_id = _bi.get('nzb_segment_id', '') or ''
                                                if _nzb_url and _nzb_url not in _seen_urls:
                                                    _blacklist_broken_nzb(_nzb_url, segment_id=_seg_id)
                                                    _seen_urls.add(_nzb_url)
                                                elif _seg_id and _nzb_url in _seen_urls:
                                                    # URL already seen but still blacklist segment if different item
                                                    from database.not_wanted_magnets import add_to_not_wanted_nzb_segment as _add_seg
                                                    _add_seg(_seg_id)
                                            # Delete from Plex using folder path directly — works even if Plex never indexed the file.
                                            # Primary: os.path.dirname(location_on_disk)
                                            # Fallback: construct path from mount + entry name when location_on_disk is NULL
                                            try:
                                                from utilities.plex_functions import scan_and_empty_plex_trash
                                                from utilities.settings import get_setting as _gs_plx
                                                import os as _osp
                                                _seen_folders = set()
                                                for _dbi in _items:
                                                    _loc = _dbi.get('location_on_disk') or ''
                                                    _is_ep = _dbi.get('type') == 'episode'
                                                    if _loc:
                                                        _folder = _osp.path.dirname(_loc)
                                                    else:
                                                        # Fallback: build path from symlink/mount base + entry name
                                                        _mode = _gs_plx('File Management', 'file_collection_management', 'Plex')
                                                        if _mode == 'Symlinked/Local':
                                                            _base = _gs_plx('File Management', 'symlinked_files_path', '').rstrip('/')
                                                            _sub = 'TV Shows' if _is_ep else 'Movies'
                                                            _folder = f'{_base}/{_sub}/{_ename}' if _base else ''
                                                        else:
                                                            _mount = _gs_plx('Usenet Provider', 'mount_path', '/debrid').rstrip('/')
                                                            _sub = 'shows' if _is_ep else 'movies'
                                                            _folder = f'{_mount}/{_sub}/{_ename}' if _mount else ''
                                                    if _folder and _folder not in _seen_folders:
                                                        _seen_folders.add(_folder)
                                                        scan_and_empty_plex_trash(
                                                            paths=[_folder],
                                                            section_type='show' if _is_ep else 'movie',
                                                            empty_trash=True,
                                                        )
                                                        logging.info(f'[NZBHealthCheck] Plex cleanup via path: {_folder!r}')
                                            except Exception as _plx_err:
                                                logging.debug(f'[NZBHealthCheck] Plex path cleanup error: {_plx_err}')
                                            # Delete from provider once (hash → name search → path fallback)
                                            _provider_deleted = _delete_from_provider(_ihash, _ename)
                                            if not _provider_deleted:
                                                # Path-based deletion fallback: remove file directly from mount
                                                for _dbi in _items:
                                                    _loc = _dbi.get('location_on_disk', '') or ''
                                                    if _loc and not _provider_deleted:
                                                        try:
                                                            import os as _os
                                                            from utilities.settings import get_setting as _gs
                                                            _mount = _gs('Usenet Provider', 'mount_path', default='').rstrip('/')
                                                            if _mount and _loc.startswith('/debrid/'):
                                                                _rel = _loc[len('/debrid/'):]
                                                                _full = f'{_mount}/{_rel}'
                                                                if _os.path.exists(_full):
                                                                    _os.remove(_full)
                                                                    logging.info(f'[NZBHealthCheck] Path-deleted broken file: {_full!r}')
                                                                    _provider_deleted = True
                                                                else:
                                                                    logging.debug(f'[NZBHealthCheck] Path delete: file not found at {_full!r}')
                                                        except Exception as _pd_err:
                                                            logging.debug(f'[NZBHealthCheck] Path delete error for {_loc!r}: {_pd_err}')
                                            for _dbi in _items:
                                                _move_to_wanted(_dbi)
                                                log_repair_activity(
                                                    item_id=_dbi.get('id'),
                                                    title=_dbi.get('title'),
                                                    media_type=_dbi.get('type'),
                                                    season_number=_dbi.get('season_number'),
                                                    episode_number=_dbi.get('episode_number'),
                                                    broken_nzb_id=_ihash,
                                                    broken_nzb_title=_ename,
                                                    outcome='plex_deleted',
                                                    triggered_by='cache_warm_error' if _broken_entries.get(_ename) == 'cache_warm' else 'webdav_error',
                                                )
                                            # Mark as handled — avoid infinite re-processing this session
                                            self._webdav_repaired_entries.add(_ename)
                                        except Exception as _ename_err:
                                            logging.error(f'[NZBHealthCheck] Webdav repair error for {_ename!r}: {_ename_err}')
                                            self._webdav_repaired_entries.add(_ename)
                                except Exception as _wr_err:
                                    logging.error(f'[NZBHealthCheck] Webdav repair import error: {_wr_err}')
            except Exception as _watcher_err:
                logging.debug(f'[NZBHealthCheck] Webdav log watcher error: {_watcher_err}')

        except Exception as e:
            logging.error(f'[NZBHealthCheck] Task error: {e}', exc_info=True)
            self._nzb_health_last_duration = 0.0

    def task_backfill_nzb_torrent_ids(self):
        """Backfill filled_by_torrent_id for collected items on cli_mount mount that have no torrent ID.
        Matches location_on_disk folder name against cli_mount /api/torrents entries."""
        try:
            import requests as _req
            from utilities.settings import get_setting
            from database.database_reading import get_all_media_items
            from database.database_writing import update_media_item

            # cli_mount-specific: this backfills filled_by_torrent_id for legacy
            # cli_mount-collected items that predate the nzb: convention, via
            # /api/torrents (which nzbdav doesn't implement). nzbdav items already
            # carry their nzb: id natively, so there is nothing to backfill — skip
            # cleanly to avoid failed requests + a misleading "Loaded 0 entries" log.
            if (get_setting('Usenet Provider', 'provider', 'climount') or 'climount').strip().lower() != 'climount':
                logging.info('[NZBBackfill] Skipped — cli_mount-specific backfill (active usenet provider is not climount).')
                return

            dcy_url = get_setting('Usenet Provider', 'url', default='').rstrip('/')
            dcy_token = get_setting('Usenet Provider', 'api_token', default='')
            if not dcy_url:
                logging.warning('[NZBBackfill] Usenet Provider URL not configured, skipping.')
                return
            headers = {'Authorization': f'Bearer {dcy_token}'} if dcy_token else {}

            # Fetch all cli_mount entries (paginated), build name→info_hash map
            logging.info('[NZBBackfill] Fetching cli_mount entries...')
            name_to_hash = {}
            page = 1
            while True:
                for attempt in range(3):
                    try:
                        r = _req.get(f'{dcy_url}/api/torrents',
                                     params={'page': page, 'limit': 100, 'sort_by': 'added_on', 'sort_order': 'desc'},
                                     headers=headers, timeout=30)
                        if r.status_code == 200:
                            break
                        logging.warning(f'[NZBBackfill] cli_mount API HTTP {r.status_code} on page {page} (attempt {attempt+1})')
                    except Exception as _pe:
                        logging.warning(f'[NZBBackfill] cli_mount API error on page {page} (attempt {attempt+1}): {_pe}')
                    if attempt == 2:
                        logging.error(f'[NZBBackfill] Giving up on page {page} after 3 attempts')
                        break
                else:
                    break
                if r.status_code != 200:
                    break
                data = r.json()
                for t in data.get('torrents', []):
                    name = (t.get('name') or '').strip()
                    info_hash = (t.get('info_hash') or '').strip()
                    if name and info_hash:
                        name_to_hash[name] = info_hash
                if not data.get('has_next'):
                    break
                page += 1

            logging.info(f'[NZBBackfill] Loaded {len(name_to_hash)} cli_mount entries.')

            # Find collected items on /debrid mount with no torrent ID or non-NZB torrent ID
            items = [dict(i) for i in get_all_media_items(state='Collected')
                     if not (i.get('filled_by_torrent_id') or '').startswith('nzb:')
                     and (i.get('location_on_disk') or '').startswith('/debrid/')]

            logging.info(f'[NZBBackfill] Found {len(items)} collected items to backfill.')

            matched = skipped = 0
            for item in items:
                loc = item.get('location_on_disk', '')
                # Extract folder name: /debrid/shows/FolderName/file.mkv → FolderName
                parts = loc.split('/')
                # parts: ['', 'debrid', 'shows'|'movies', 'FolderName', ...]
                if len(parts) < 4:
                    skipped += 1
                    continue
                folder_name = parts[3]
                if not folder_name:
                    skipped += 1
                    continue

                info_hash = name_to_hash.get(folder_name)
                # If folder is already in NZB format (contains {imdb-), extract the original
                # release name from the trailing (...) and try that against cli_mount's old names.
                if not info_hash and '{imdb-' in folder_name:
                    import re as _re
                    m = _re.search(r'\(([^)]+)\)\s*$', folder_name)
                    if m:
                        orig_from_folder = m.group(1)
                        info_hash = name_to_hash.get(orig_from_folder)
                # Try filled_by_file (with and without ext)
                if not info_hash:
                    fbf = item.get('filled_by_file') or ''
                    if fbf:
                        fbf_noext = fbf.rsplit('.', 1)[0] if '.' in fbf else fbf
                        info_hash = name_to_hash.get(fbf) or name_to_hash.get(fbf_noext)
                        # If fbf is also NZB format, extract its trailing original name too
                        if not info_hash and '{imdb-' in fbf_noext:
                            m = _re.search(r'\(([^)]+)\)\s*$', fbf_noext)
                            if m:
                                info_hash = name_to_hash.get(m.group(1))
                if not info_hash:
                    orig = item.get('original_scraped_torrent_title') or ''
                    if orig:
                        info_hash = name_to_hash.get(orig)
                if info_hash:
                    update_media_item(item['id'], filled_by_torrent_id=f'nzb:{info_hash}')
                    matched += 1
                else:
                    if skipped < 10:
                        fbf = item.get('filled_by_file') or ''
                        orig = item.get('original_scraped_torrent_title') or ''
                        logging.info(f'[NZBBackfill] Unmatched: id={item.get("id")} loc={loc!r} folder={folder_name!r} fbf={fbf!r} orig={orig!r}')
                    skipped += 1

            logging.info(f'[NZBBackfill] Complete: {matched} matched, {skipped} unmatched/skipped.')

            # --- Phase 2: Backfill nzb_segment_id for items that have filled_by_magnet but no segment ID ---
            # Fetches the NZB XML from the stored URL and extracts the first segment Message-ID.
            # Rate-limited to avoid hammering indexers — 1 request per item.
            try:
                import requests as _req2
                from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg
                seg_items = [
                    dict(i) for i in get_all_media_items(state='Collected')
                    if (i.get('filled_by_magnet') or '').startswith('http')
                    and not (i.get('nzb_segment_id') or '').strip()
                ]
                logging.info(f'[NZBBackfill] Phase 2: {len(seg_items)} items need nzb_segment_id backfill.')
                seg_matched = seg_skipped = 0
                for seg_item in seg_items:
                    try:
                        _url = seg_item.get('filled_by_magnet', '')
                        _r = _req2.get(_url, timeout=15, allow_redirects=True,
                                       headers={'User-Agent': 'Sabnzbd/3.0.0'})
                        if _r.status_code == 200 and '<nzb' in _r.text.lower():
                            _seg_id = _ext_seg(_r.text)
                            if _seg_id:
                                update_media_item(seg_item['id'], nzb_segment_id=_seg_id)
                                seg_matched += 1
                                continue
                    except Exception:
                        pass
                    seg_skipped += 1
                logging.info(f'[NZBBackfill] Phase 2 complete: {seg_matched} segment IDs backfilled, {seg_skipped} skipped.')
            except Exception as _seg_err:
                logging.warning(f'[NZBBackfill] Phase 2 error: {_seg_err}')

        except Exception as e:
            logging.error(f'[NZBBackfill] Task error: {e}', exc_info=True)

    def task_nzb_playback_repair_completion(self):
        """Verify and clean only active NZB playback repairs; never scans the backlog."""
        try:
            from database.nzb_playback_repair import process_pending_playback_repairs
            process_pending_playback_repairs()
        except Exception as exc:
            logging.error('[NZBPlayback] Completion task failed: %s', exc, exc_info=True)

    def task_nzb_playback_cleanup_retry(self):
        """Slow-cadence background retry for old-file cleanup deferred by the
        fast completion worker — repairs already finalized as replaced."""
        try:
            from database.nzb_playback_repair import retry_deferred_playback_cleanups
            retry_deferred_playback_cleanups()
        except Exception as exc:
            logging.error('[NZBPlayback] Cleanup retry task failed: %s', exc, exc_info=True)

    def task_repair_broken_nzbs(self, triggered_by: str = 'scheduled'):
        """Scan cli_mount for broken NZBs and attempt to repair them via re-scrape."""
        logging.info('[NZBRepair] Starting broken NZB repair task')
        try:
            # Trigger a partial NZB health scan and wait for it to complete
            try:
                from usenet.repair_engine import trigger_health_scan
                trigger_health_scan(full=False, wait=True, timeout=300)
                logging.info('[NZBRepair] Pre-scan complete')
            except Exception as scan_err:
                logging.warning(f'[NZBRepair] Pre-scan failed (continuing anyway): {scan_err}')
            from usenet.repair_engine import run_repair
            summary = run_repair(triggered_by=triggered_by)
            logging.info(
                f'[NZBRepair] Task complete — broken={summary["broken_found"]}, '
                f'matched={summary["matched"]}, replaced={summary["replaced"]}, '
                f'no_replacement={summary.get("no_replacement", 0)}, '
                f'submission_failed={summary.get("submission_failed", 0)}, '
                f'skipped_backoff={summary.get("skipped_backoff", 0)}, '
                f'skipped_max={summary.get("skipped_max_attempts", 0)}, '
                f'not_found={summary["not_found"]}, errors={summary["errors"]}'
            )
        except Exception as e:
            logging.error(f'[NZBRepair] Task error: {e}', exc_info=True)

    def task_repair_broken_debrids(self, triggered_by: str = 'scheduled'):
        """Scan cli_mount for broken torrent entries and attempt to repair them via CLI re-insertion."""
        logging.info('[DebridRepair] Starting broken debrid repair task')
        try:
            # Clean ghost health records first so repair doesn't waste time on them
            try:
                from usenet.debrid_repair_engine import delete_ghost_health_records
                ghost_result = delete_ghost_health_records()
                if ghost_result['deleted']:
                    logging.info(f'[DebridRepair] Ghost cleanup: deleted={ghost_result["deleted"]}')
            except Exception as ghost_err:
                logging.warning(f'[DebridRepair] Ghost cleanup failed (continuing): {ghost_err}')

            # Trigger a partial torrent health scan and wait for it to complete
            try:
                from usenet.debrid_repair_engine import trigger_health_scan
                trigger_health_scan(full=False, wait=True, timeout=300)
                logging.info('[DebridRepair] Pre-scan complete')
            except Exception as scan_err:
                logging.warning(f'[DebridRepair] Pre-scan failed (continuing anyway): {scan_err}')
            from usenet.debrid_repair_engine import run_repair
            summary = run_repair(triggered_by=triggered_by)
            logging.info(
                f'[DebridRepair] Task complete — broken={summary["broken_found"]}, '
                f'reinserted={summary["reinserted"]}, replaced={summary["replaced"]}, '
                f'not_found={summary["not_found"]}, errors={summary["errors"]}'
            )
        except Exception as e:
            logging.error(f'[DebridRepair] Task error: {e}', exc_info=True)

    def task_sync_cli_mount_changes(self):
        """Poll cli_mount for entry changes and sync key fields into CLI media_items DB."""
        from usenet.climount_sync import sync_changes_from_climount, _get_last_sync_ts
        is_first_run = _get_last_sync_ts() == 0
        if is_first_run:
            self.pause_info = {
                'reason_string': 'cli_mount initial full sync in progress — runs once on first setup, queue resumes automatically',
                'error_type': 'SYSTEM_MAINTENANCE',
                'service_name': 'cli_mount sync',
                'status_code': None,
                'retry_count': 0,
            }
            self.pause_queue()
            logging.info('[CMSync] First run — queue paused during initial full sync')
        try:
            sync_changes_from_climount()
        except Exception as e:
            logging.error(f'[CMSync] Task error: {e}', exc_info=True)
        finally:
            if is_first_run:
                self.last_resume_time = None  # bypass 30s throttle
                self.pause_info = {'reason_string': None, 'error_type': None,
                                   'service_name': None, 'status_code': None, 'retry_count': 0}
                self.resume_queue()
                logging.info('[CMSync] Initial full sync complete — queue resumed')

    def task_scan_mount_for_external_adds(self):
        """Import content that appeared in the debrid mount without cli_debrid putting it there.

        Covers mount providers with no library-update webhook (Decypharr, plain
        rclone). Zurg calls /webhook/rclone via on_library_update instead, which
        runs the same import pipeline.
        """
        from utilities.external_mount_scan import scan_mount_for_external_adds
        try:
            summary = scan_mount_for_external_adds()
            if summary.get('skipped_reason'):
                logging.debug(f"[ExternalScan] Skipped: {summary['skipped_reason']}")
            elif summary.get('baselined'):
                logging.info(f"[ExternalScan] Baselined {summary['baselined']} existing mount entries.")
            elif summary.get('imported') or summary.get('failed') or summary.get('rejected'):
                logging.info(
                    f"[ExternalScan] Scanned {summary.get('entries_seen', 0)} entries — "
                    f"imported {summary.get('imported', 0)}, failed {summary.get('failed', 0)}, "
                    f"skipped {summary.get('rejected', 0)} as previously rejected."
                )
        except Exception as e:
            logging.error(f"[ExternalScan] Task error: {e}", exc_info=True)

    def task_push_pending_climount_tags(self):
        """Push tags for media_items rows whose tags changed on the cli_debrid
        side only — sync_changes_from_climount can't detect these since it only
        re-fetches entries cli_mount itself has changed."""
        from usenet.climount_sync import push_pending_tags
        try:
            push_pending_tags()
        except Exception as e:
            logging.error(f'[CMSync] push_pending_tags task error: {e}', exc_info=True)
