"""Metadata tasks: release dates, show/movie IDs and titles, TV status, airing shows.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
from content_checkers.trakt import check_trakt_early_releases
import logging
from cli_battery.app.direct_api import DirectAPI
from routes.api_tracker import api
from datetime import datetime
from database.core import get_db_connection
import sqlite3
import time
from datetime import timezone


class MetadataTasks:
    def task_refresh_release_dates(self):
        from metadata.metadata import refresh_release_dates # Added import here
        refresh_release_dates()

    def task_check_trakt_early_releases(self):
        check_trakt_early_releases()

    def task_update_show_ids(self):
        """Update show IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
        try:
            from database.maintenance import update_show_ids
            update_show_ids()
        except Exception as e:
            logging.error(f"Error in task_update_show_ids: {str(e)}")

    def task_update_show_titles(self):
        """Update show titles in the database if they don't match the direct API, storing old titles in title_aliases."""
        try:
            from database.maintenance import update_show_titles
            update_show_titles()
        except Exception as e:
            logging.error(f"Error in task_update_show_titles: {str(e)}")

    def task_update_movie_ids(self):
        """Update movie IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
        try:
            from database.maintenance import update_movie_ids
            update_movie_ids()
        except Exception as e:
            logging.error(f"Error in task_update_movie_ids: {str(e)}")

    def task_update_movie_titles(self):
        """Update movie titles in the database if they don't match the direct API, storing old titles in title_aliases."""
        try:
            from database.maintenance import update_movie_titles
            update_movie_titles()
        except Exception as e:
            logging.error(f"Error in task_update_movie_titles: {str(e)}")

    def task_sync_episode_metadata(self):
        """Sync episode metadata (titles, etc.) from Trakt/TMDB for all collected shows."""
        try:
            from database.maintenance import sync_episode_metadata
            sync_episode_metadata()
        except Exception as e:
            logging.error(f"Error in task_sync_episode_metadata: {str(e)}")

    def task_cleanup_title_year_suffixes(self):
        """Database year-in-title cleanup — strips trailing (YYYY) from title column."""
        try:
            from database.maintenance import cleanup_title_year_suffixes
            cleanup_title_year_suffixes()
        except Exception as e:
            logging.error(f"Error in task_cleanup_title_year_suffixes: {str(e)}")

    # --- START: New Task Implementation ---
    def task_update_tv_show_status(self):
        """
        Periodically updates the status for TV shows in the tv_shows table
        based on external metadata, and calculates per-version presence status
        in the tv_show_version_status table based on local collection state.
        """
        logging.info("[TASK] Running TV show status update...")
        start_time = time.time()
        conn = None
        updated_count = 0
        failed_count = 0
        processed_shows = set() # Track shows processed in this run
        shows_with_versions_updated = set() # Track shows where versions were processed

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Get distinct show IMDB IDs from media_items (episodes only needed now)
            cursor.execute("""
                SELECT DISTINCT imdb_id
                FROM media_items
                WHERE type = 'episode' AND imdb_id IS NOT NULL AND imdb_id != '' AND season_number > 0
            """)
            show_imdb_ids = [row['imdb_id'] for row in cursor.fetchall()]

            if not show_imdb_ids:
                logging.info("[TV Status] No TV show IMDB IDs found in media_items (episodes) to update.")
                return

            logging.info(f"[TV Status] Found {len(show_imdb_ids)} unique show IMDB IDs with episodes to check.")
            api = DirectAPI()

            for imdb_id in show_imdb_ids:
                if imdb_id in processed_shows:
                    continue

                logging.debug(f"[TV Status] Processing show: {imdb_id}")
                show_metadata = None
                source = "Unknown"
                show_status = 'unknown' # Default status
                total_episodes_from_source = 0
                total_seasons_from_source = 0
                is_show_ended = False
                tmdb_id = None
                title = None
                year = None

                try:
                    # Fetch metadata using DirectAPI
                    show_metadata, source = api.get_show_metadata(imdb_id)

                    if not show_metadata:
                        logging.warning(f"[TV Status] No metadata found for show {imdb_id} from source '{source}'. Will proceed with version check using existing DB status if available.")
                        # Try to get existing status from DB to determine if ended
                        cursor.execute("SELECT status, total_episodes, total_seasons FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                        existing_show = cursor.fetchone()
                        if existing_show:
                            show_status = existing_show['status'].lower() if existing_show['status'] else 'unknown'
                            total_episodes_from_source = existing_show['total_episodes'] or 0
                            total_seasons_from_source = existing_show['total_seasons'] or 0
                        else:
                             # No metadata and no existing record, cannot determine version completeness accurately
                             logging.warning(f"[TV Status] No existing record for {imdb_id} either. Skipping version status calculation.")
                             failed_count += 1
                             processed_shows.add(imdb_id)
                             continue # Skip to next show
                    else:
                        # Process metadata if found
                        show_status = show_metadata.get('status', 'unknown').lower()
                        tmdb_id = show_metadata.get('ids', {}).get('tmdb')
                        title = show_metadata.get('title')
                        year = show_metadata.get('year')

                        # Calculate total episodes and seasons from source metadata
                        if 'seasons' in show_metadata:
                             # Reset count before summing seasons
                            total_episodes_from_source = 0
                            total_seasons_from_source = 0
                            for season_num_str, season_data in show_metadata.get('seasons', {}).items():
                                try:
                                     season_num = int(season_num_str)
                                     if season_num == 0: continue # Skip specials season
                                     total_episodes_from_source += len(season_data.get('episodes', {}))
                                     total_seasons_from_source += 1
                                except ValueError:
                                     logging.warning(f"[TV Status] Invalid season number '{season_num_str}' in metadata for {imdb_id}. Skipping.")
                                     continue
                        else:
                            logging.warning(f"[TV Status] Metadata for {imdb_id} ('{title}') lacks 'seasons' key. Total episode count may be inaccurate.")
                            # Fallback to DB value if exists? Or treat as 0? Let's fetch existing.
                            cursor.execute("SELECT total_episodes, total_seasons FROM tv_shows WHERE imdb_id = ?", (imdb_id,))
                            existing_show_fallback = cursor.fetchone()
                            total_episodes_from_source = existing_show_fallback['total_episodes'] if existing_show_fallback else 0
                            total_seasons_from_source = existing_show_fallback['total_seasons'] if existing_show_fallback else 0
                            if total_episodes_from_source == 0:
                                logging.warning(f"[TV Status] No episode count from metadata or DB for {imdb_id}. Skipping version status calculation.")
                                # We can still update show status, but version logic is impossible


                    # Determine overall show ended status based *only* on metadata status
                    # Treat 'canceled' the same as 'ended' for completion purposes
                    is_show_ended = bool(show_status in ('ended', 'canceled'))

                    logging.debug(f"[TV Status] Show: {imdb_id} ('{title}') - Status: {show_status}, Source Episodes: {total_episodes_from_source}, IsEnded/Canceled: {is_show_ended}")

                    # Prepare data for tv_shows DB update/insert
                    now_utc = datetime.now(timezone.utc)
                    now_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')

                    # Upsert into tv_shows. 'is_complete' reflects if the show's status is 'ended'/'canceled'.
                    # total_episodes is updated from source metadata.
                    # Ensure COALESCE is used for fields that might not be present in new metadata fetch
                    # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior
                    cursor.execute("""
                        INSERT INTO tv_shows (
                            imdb_id, tmdb_id, title, year, status, is_complete,
                            total_episodes, total_seasons, last_status_check, added_at, last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT added_at FROM tv_shows WHERE imdb_id = ?), ?), ?)
                        ON CONFLICT DO UPDATE SET
                            tmdb_id = COALESCE(NULLIF(excluded.tmdb_id, ''), tv_shows.tmdb_id),
                            title = COALESCE(NULLIF(excluded.title, ''), tv_shows.title),
                            year = COALESCE(excluded.year, tv_shows.year),
                            status = COALESCE(NULLIF(excluded.status, ''), tv_shows.status),
                            is_complete = excluded.is_complete,
                            total_episodes = excluded.total_episodes,
                            total_seasons = excluded.total_seasons,
                            last_status_check = excluded.last_status_check,
                            last_updated = excluded.last_updated
                        WHERE tv_shows.imdb_id = excluded.imdb_id;
                    """, (
                        imdb_id, tmdb_id, title, year, show_status, int(is_show_ended),
                        total_episodes_from_source, total_seasons_from_source, now_str, # last_status_check
                        # Values for INSERT part (added_at logic)
                        imdb_id, now_str, # imdb_id for subquery, now_str for COALESCE fallback
                        # Value for INSERT part (last_updated)
                        now_str
                    ))
                    conn.commit() # Commit show data before processing versions
                    updated_count += 1 # Count successful show upsert

                    # --- NEW: Per-Version Status Update ---
                    # Skip if we couldn't determine total episodes and show is ended/canceled
                    # (Can't reliably calculate completeness)
                    if total_episodes_from_source <= 0 and is_show_ended:
                         logging.warning(f"[TV Status] Cannot reliably calculate version completeness for ended/canceled show {imdb_id} due to zero total episodes. Skipping version updates.")
                    else:
                        try:
                            # Get all episode items with their version and state for this show
                            cursor.execute("""
                                SELECT state, version -- Fetch the version key directly
                                FROM media_items
                                WHERE imdb_id = ? AND type = 'episode' AND season_number > 0
                            """, (imdb_id,))
                            all_episodes = cursor.fetchall()

                            if not all_episodes:
                                logging.debug(f"[TV Status] No local episode media items found for {imdb_id}. Cleaning up old version statuses.")
                                # Remove any stale version statuses if no episodes exist anymore
                                cursor.execute("DELETE FROM tv_show_version_status WHERE imdb_id = ?", (imdb_id,))
                            else:
                                # Group episodes by version identifier
                                episodes_by_version = {}
                                for episode in all_episodes:
                                    version_identifier = (episode['version'] or 'UnknownVersion').rstrip('*') # Handle potential NULL/empty version and trim trailing '*'
                                    if version_identifier not in episodes_by_version:
                                        episodes_by_version[version_identifier] = []
                                    episodes_by_version[version_identifier].append(episode)

                                logging.debug(f"[TV Status] Found {len(episodes_by_version)} versions for {imdb_id}: {list(episodes_by_version.keys())}")

                                # Process each version
                                version_now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                                versions_processed_this_show = set()
                                for version_id, episodes_in_version in episodes_by_version.items():
                                    versions_processed_this_show.add(version_id)
                                    # Count present episodes (Collected or Blacklisted)
                                    present_count = sum(1 for ep in episodes_in_version if ep['state'] in ('Collected', 'Blacklisted'))

                                    # Determine if this version is up-to-date (has all known episodes)
                                    # If show is not ended, up-to-date is meaningless, default to False? Or base on present_count?
                                    # Let's define up-to-date ONLY if the show is ended/canceled AND has all eps.
                                    is_up_to_date = bool(
                                        is_show_ended and # Show must be finished
                                        total_episodes_from_source > 0 and
                                        present_count >= total_episodes_from_source
                                    )

                                    # Determine if this version is complete AND fully present
                                    # is_complete_and_present is essentially the same as is_up_to_date by this definition
                                    is_complete_and_present = is_up_to_date

                                    logging.debug(f"[TV Status] Version '{version_id}' for {imdb_id}: Present: {present_count}/{total_episodes_from_source}, ShowEnded/Canceled: {is_show_ended} -> UpToDate: {is_up_to_date}, CompleteAndPresent: {is_complete_and_present}")

                                    # Upsert into the new version status table
                                    version_data = (
                                        imdb_id,
                                        version_id,
                                        int(is_complete_and_present), # Store as integer 0 or 1
                                        int(is_up_to_date),           # Store as integer 0 or 1
                                        present_count,
                                        version_now_str # last_checked
                                    )
                                    cursor.execute("""
                                        INSERT INTO tv_show_version_status (
                                            imdb_id, version_identifier, is_complete_and_present,
                                            is_up_to_date, present_episode_count, last_checked
                                        ) VALUES (?, ?, ?, ?, ?, ?)
                                        ON CONFLICT(imdb_id, version_identifier) DO UPDATE SET
                                            is_complete_and_present = excluded.is_complete_and_present,
                                            is_up_to_date = excluded.is_up_to_date,
                                            present_episode_count = excluded.present_episode_count,
                                            last_checked = excluded.last_checked;
                                    """, version_data)

                                # Clean up version statuses for versions that no longer exist locally
                                if versions_processed_this_show: # Ensure set is not empty before query
                                     cursor.execute("""
                                        DELETE FROM tv_show_version_status
                                        WHERE imdb_id = ? AND version_identifier NOT IN ({})
                                    """.format(','.join('?'*len(versions_processed_this_show))), (imdb_id, *versions_processed_this_show))
                                else:
                                     # If somehow versions_processed_this_show is empty after finding episodes, delete all for this imdb_id
                                     logging.warning(f"[TV Status] No versions processed for {imdb_id} despite finding episodes. Cleaning all version statuses.")
                                     cursor.execute("DELETE FROM tv_show_version_status WHERE imdb_id = ?", (imdb_id,))


                                shows_with_versions_updated.add(imdb_id)

                            conn.commit() # Commit version status updates for this show

                        except sqlite3.Error as db_err_version:
                            logging.error(f"[TV Status] Database error during version status update for {imdb_id}: {db_err_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1 # Count show as failed if version update fails
                            # Rollback removed the main show update, so no need to adjust updated_count
                            updated_count -= 1 # Decrement successful show update count
                        except Exception as e_version:
                            logging.error(f"[TV Status] Error during version status update for {imdb_id}: {e_version}", exc_info=True)
                            if conn: conn.rollback()
                            failed_count += 1
                            updated_count -= 1 # Decrement successful show update count


                    processed_shows.add(imdb_id) # Mark base show info as processed (even if version failed)

                except Exception as e:
                    logging.error(f"[TV Status] Failed to process show {imdb_id}: {e}", exc_info=True)
                    failed_count += 1
                    if conn: conn.rollback() # Rollback any partial changes for this show
                    processed_shows.add(imdb_id) # Mark as processed to avoid retrying in this run
                    # Ensure it's not counted as having versions updated
                    if imdb_id in shows_with_versions_updated:
                        shows_with_versions_updated.remove(imdb_id)


            # No final commit needed here as commits happen per-show or are rolled back on error

        except sqlite3.Error as db_err:
            logging.error(f"[TV Status] Database error during TV show status update setup: {db_err}", exc_info=True)
            if conn: conn.rollback()
        except Exception as err:
            logging.error(f"[TV Status] Unexpected error during TV show status update: {err}", exc_info=True)
            if conn: conn.rollback() # Rollback any potential transaction
        finally:
            if conn:
                conn.close()

        duration = time.time() - start_time
        # Refined counting:
        # processed_shows = count of unique imdb_ids attempted.
        # updated_count = count of successful base show info upserts.
        # shows_with_versions_updated = count where version logic also completed.
        # failed_count = count where either base or version logic failed with error/rollback.
        logging.info(f"[TASK] TV show status update finished in {duration:.2f}s. Processed Shows: {len(processed_shows)}, Base Info Updated: {updated_count}, Versions Updated: {len(shows_with_versions_updated)}, Failed: {failed_count}.")

    def task_precompute_airing_shows(self):
        """Precompute the recently aired and airing soon shows in a background task"""
        try:
            from routes.statistics_routes import get_recently_aired_and_airing_soon

            # Actually call the function to populate the cache
            logging.info("Precomputing airing shows data...")
            start_time = time.time()
            # Call the function without the unsupported force_refresh argument
            recently_aired, airing_soon = get_recently_aired_and_airing_soon()

            duration = time.time() - start_time
            logging.info(f"Precomputed airing shows data in {duration:.2f}s. Found {len(recently_aired)} recently aired and {len(airing_soon)} airing soon shows.")
        except Exception as e:
            logging.error(f"Error precomputing airing shows: {e}")
