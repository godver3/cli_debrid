"""Library upkeep: symlink verification, maintenance, subtitles, overlays, collections, media analysis.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
import logging
from utilities.settings import get_setting
import os


class LibraryTasks:
    def task_run_library_maintenance(self):
        """Run library maintenance tasks."""
        from database.maintenance import run_library_maintenance
        run_library_maintenance()

    def task_verify_symlinked_files(self):
        """Verify symlinked files have been properly scanned into Plex."""
        logging.info("Checking for symlinked files to verify in Plex...")
        try:
            # Import here to avoid circular imports
            from database.symlink_verification import get_verification_stats
            from utilities.plex_verification import run_plex_verification_scan

            # Check if there are any unverified files to process
            stats = get_verification_stats()
            if stats['unverified'] == 0:
                logging.info("No unverified files in queue. Skipping verification scan.")
                return

            # Alternate between full and recent scans
            # Use a class attribute to track the last scan type
            if not hasattr(self, '_last_symlink_scan_was_full'):
                # Initialize to True so first scan will be recent (gets toggled below)
                self._last_symlink_scan_was_full = True

            # Toggle scan type
            do_full_scan = not self._last_symlink_scan_was_full
            self._last_symlink_scan_was_full = do_full_scan

            scan_type = "full" if do_full_scan else "recent"
            logging.info(f"Running {scan_type} symlink verification scan...")

            # Run the verification scan
            verified_count, total_processed = run_plex_verification_scan(
                max_files=500,
                recent_only=not do_full_scan
            )

            logging.info(f"Verified {verified_count} out of {total_processed} symlinked files in Plex ({scan_type} scan)")

            # If recent scan found nothing but we have unverified files, force a full scan next time
            if not do_full_scan and total_processed == 0 and stats['unverified'] > 0:
                logging.info("Recent scan found no files but unverified files exist. Will run full scan next time.")
                self._last_symlink_scan_was_full = False

        except Exception as e:
            logging.error(f"Error verifying symlinked files: {e}")

        # Sync ms_item_id for any Collected items missing it
        try:
            from overlays.scheduled_tasks import _sync_ms_keys_auto
            counts = _sync_ms_keys_auto()
            if counts and any(counts.values()):
                logging.info(f"[SyncMsItemIds] Updated ms_item_id: movies={counts.get('movies', 0)}, "
                             f"episodes={counts.get('episodes', 0)}, errors={counts.get('errors', 0)}")
        except Exception as e:
            logging.debug(f"[SyncMsItemIds] ms_item_id sync skipped: {e}")

    # Add media analysis task method
    def task_analyze_media_files(self):
        """Scheduled task to analyze and repair media files."""
        from utilities.analyze_library import analyze_and_repair_media_files

        logging.info("Initiating scheduled media file analysis and repair task.")
        try:
            collection_type = get_setting('File Management', 'file_collection_management', 'Plex')
            if collection_type not in ['Plex', 'Symlinked/Local']:
                logging.warning(
                    f"Unsupported collection type '{collection_type}' for media analysis. Supported types are 'Plex' or 'Symlinked/Local'. Skipping."
                )
                return

            # The analyze_and_repair_media_files function uses its own default for max_files_to_check_this_run
            # If you want to make this configurable via settings.ini for the scheduled task,
            # you could add:
            # max_files_setting = get_setting('Maintenance', 'media_analysis_max_files_per_run', <default_from_analyze_library>)
            # and pass it to the function: analyze_and_repair_media_files(collection_type, max_files_setting)

            analyze_and_repair_media_files(collection_type=collection_type)
            logging.info("Scheduled media file analysis and repair task completed.")

        except Exception as e:
            logging.error(f"Error during scheduled media file analysis and repair: {e}", exc_info=True)

    # Add bulk subtitle processing task
    def task_process_bulk_subtitles(self):
        """Scheduled task: search subtitles for collected items that are missing
        a configured language (both file management modes).

        Items already having every language (sidecar files, or what cli_debrid
        uploaded to Plex) are skipped, as are items searched in the last 24 h
        without a result, so each run doesn't search the same items again.
        Capped at 200 items per run; providers that hit their download limit
        are paused for the rest of the run.
        """
        from utilities.settings import get_setting
        if not get_setting('Subtitle Settings', 'enable_subtitles', False):
            logging.info("[BulkSubs] Subtitle downloads are off (Settings → Subtitles); nothing to do.")
            return
        try:
            from utilities import subtitle_bulk, subtitle_status
            items = subtitle_status.missing_items(skip_tried_hours=24, limit=200)
            if not items:
                logging.info("[BulkSubs] No collected items are missing subtitles.")
                return
            logging.info(f"[BulkSubs] Searching subtitles for {len(items)} item(s) missing a language.")
            subtitle_bulk.run_now(items, label='Bulk subtitles task', kind='search')
        except Exception as e:
            logging.error(f"Error during bulk subtitle processing: {e}", exc_info=True)

    def task_overlay_sync(self):
        """Scheduled task to sync overlay data and regenerate changed posters."""
        try:
            from overlays.scheduled_tasks import task_overlay_sync as sync_func
            with self._running_task_lock:
                _triggered_by = 'manual' if any(
                    'manual_task_overlay_sync' in jid
                    for jid in self.currently_executing_tasks
                ) else 'scheduled'
            result = sync_func(triggered_by=_triggered_by)
            if result.get('success'):
                logging.info(f"Overlay sync completed: {result.get('message', 'Success')}")
            else:
                logging.warning(f"Overlay sync had issues: {result.get('message', 'Unknown error')}")
        except ImportError as e:
            logging.warning(f"Overlay module not available: {e}")
        except Exception as e:
            logging.error(f"Error in overlay sync task: {e}", exc_info=True)
        finally:
            # Overlay sync loads large amounts of Plex metadata into memory.
            # Force glibc to return freed arena pages to the OS immediately.
            try:
                import gc, ctypes
                def _rss_kb():
                    with open('/proc/self/status') as _f:
                        for _l in _f:
                            if _l.startswith('VmRSS:'):
                                return int(_l.split()[1])
                    return 0
                _before_rss = _rss_kb()
                gc.collect()
                ctypes.CDLL('libc.so.6').malloc_trim(0)
                _after_rss = _rss_kb()
                logging.info(f"[OVERLAY_SYNC] malloc_trim: RSS {_before_rss//1024} MB → {_after_rss//1024} MB (freed {(_before_rss - _after_rss)//1024} MB)")
            except Exception as _e:
                logging.info(f"[OVERLAY_SYNC] malloc_trim skipped: {_e}")

    def task_overlay_cleanup(self):
        """Scheduled task to clean up unused posters and overlay cache."""
        try:
            logging.info("Overlay cleanup task starting")
            db_path = os.environ.get('DATABASE_PATH', '/user/db_content/media_items.db')

            # 1. Clean up orphaned poster backup files
            try:
                from overlays.cache_cleanup import PosterCacheManager
                manager = PosterCacheManager(db_path)
                backup_result = manager.cleanup_orphaned_backups()
                removed = backup_result.get('orphaned_removed', 0)
                logging.info(f"Overlay cleanup: removed {removed} orphaned backup file(s)")
            except Exception as e:
                logging.warning(f"Overlay cleanup: backup cleanup failed: {e}")

            # 2. Reset stuck 'analyzing' states (older than 30 minutes) to 'pending'
            try:
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(db_path)
                conn.execute("""
                    UPDATE media_overlay_state SET status = 'pending'
                    WHERE status = 'analyzing'
                      AND updated_at < datetime('now', '-30 minutes')
                """)
                stuck_reset = conn.total_changes
                conn.commit()
                conn.close()
                if stuck_reset:
                    logging.info(f"Overlay cleanup: reset {stuck_reset} stuck analyzing state(s) to pending")
            except Exception as e:
                logging.warning(f"Overlay cleanup: stuck state reset failed: {e}")

            # 3. DB housekeeping + Plex poster pruning (unified in task_overlay_cleanup)
            from overlays.scheduled_tasks import task_overlay_cleanup as cleanup_func
            with self._running_task_lock:
                _triggered_by = 'manual' if any(
                    'manual_task_overlay_cleanup' in jid
                    for jid in self.currently_executing_tasks
                ) else 'scheduled'
            result = cleanup_func(triggered_by=_triggered_by)
            if result.get('success'):
                logging.info(f"Overlay cleanup completed: {result.get('message', 'Success')}")
            else:
                logging.warning(f"Overlay cleanup had issues: {result.get('message', 'Unknown error')}")

        except ImportError as e:
            logging.warning(f"Overlay module not available: {e}")
        except Exception as e:
            logging.error(f"Error in overlay cleanup task: {e}", exc_info=True)

    def task_plex_smart_collection_posters(self):
        """Apply custom posters to enabled Plex smart collections."""
        try:
            from database.plex_smart_collections import apply_smart_collection_posters
            apply_smart_collection_posters()
        except Exception as e:
            logging.error(f"Plex smart collection posters task failed: {e}", exc_info=True)

    def task_plex_movie_boxsets(self):
        """Sync Plex movie box set collections from TMDB collection data."""
        try:
            from database.plex_movie_boxsets import run_plex_movie_boxsets
            run_plex_movie_boxsets()
        except Exception as e:
            logging.error(f"Plex movie box sets task failed: {e}", exc_info=True)
