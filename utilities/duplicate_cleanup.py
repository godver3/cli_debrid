"""Find and remove duplicate media_items rows.

Handles failed upgrades (an item left both Collected and Blacklisted), the
reverse blacklist bug, ghostlisted duplicates, and multiple Collected versions
of the same title, for movies and shows. Used by Debug Functions ->
"Cleanup duplicates".
"""

import logging

from debrid import get_debrid_provider
from utilities.deletion_manager import DeletionManager
from utilities.settings import get_setting

# Global progress tracking for SSE
cleanup_progress = {}


def cleanup_duplicate_versions(dry_run=False, version_match='all', media_type='movie',
                              keep_action='keep_collected', nas_filter='all', exclude_patterns=None,
                              program_runner=None):
    """Remove duplicate entries - handles failed upgrades and multiple collected versions.

    Supports both movies and TV shows with three modes:

    1. Keep Collected, Delete Blacklisted (for failed upgrades):
       Handles items with both Collected and Blacklisted versions

    2. Keep Blacklisted, Delete Collected (for blacklist bug):
       Reverses the action to fix blacklist bugs

    3. Keep Best Quality, Delete Rest (NEW - for multiple collected):
       When multiple collected versions exist, keeps the one with the highest
       current_score and deletes the rest. Uses resolution-based fallback if
       scores are NULL (2160p > 1080p > 720p > 480p > SD).

    Returns (response_body, http_status).

    Args:
        dry_run: If True, only preview what would be deleted
        version_match: If 'same', only delete items with matching version. If 'all', delete all.
        media_type: 'movie' or 'show' (default: 'movie')
        keep_action: 'keep_collected', 'keep_blacklisted', 'keep_collected_delete_ghostlisted',
                     'keep_ghostlisted_delete_collected', or 'keep_best_quality' (default: 'keep_collected')
        nas_filter: 'all', 'exclude_nas' or 'only_nas'
        exclude_patterns: filename substrings; matching items are never deleted
        program_runner: the running ProgramRunner, paused while items are deleted
    """
    from database.core import get_db_connection
    import time

    conn = None
    try:

        from utilities.settings import get_nas_paths, is_nas_path
        nas_paths = get_nas_paths()

        def _group_is_nas(versions_list):
            """Return True if any item in the group has a NAS location_on_disk."""
            return any(is_nas_path(v.get('location_on_disk') or '', nas_paths) for v in versions_list)

        def _skip_for_nas_filter(versions_list):
            """Return True if this group should be skipped based on the NAS filter setting."""
            if nas_filter == 'all' or not nas_paths:
                return False
            group_is_nas = _group_is_nas(versions_list)
            if nas_filter == 'exclude_nas' and group_is_nas:
                return True
            if nas_filter == 'only_nas' and not group_is_nas:
                return True
            return False

        exclude_patterns = exclude_patterns or []
        if exclude_patterns:
            logging.info(f"[CLEANUP_DUPLICATES] Exclude patterns: {exclude_patterns}")

        # Helper function to check if item should be excluded from deletion
        def should_exclude_item(item, patterns):
            """Check if item matches any exclusion pattern (case-insensitive substring matching)"""
            if not patterns:
                return False, None

            # Get filename from item (first non-null)
            filename = (
                item.get('filled_by_file') or
                item.get('location_basename') or
                item.get('location_on_disk') or
                ''
            )

            if not filename:
                return False, None

            # Case-insensitive substring matching
            filename_lower = filename.lower()
            for pattern in patterns:
                if pattern.lower() in filename_lower:
                    return True, pattern

            return False, None

        conn = get_db_connection()
        cursor = conn.cursor()
        start_time = time.time()

        # Initialize queue pause tracking
        paused_queue = False
        program_runner = None

        # Determine the type filter for SQL
        if media_type == 'show':
            type_filter = "type = 'episode'"
            media_label = "shows"
        else:  # movie
            type_filter = "type = 'movie'"
            media_label = "movies"

        # Handle keep_best_quality mode separately (finds items with multiple collected versions)
        if keep_action == 'keep_best_quality':
            # Find items with multiple collected (non-ghostlisted) versions
            # Uses TWO-PASS approach via UNION to catch duplicates by EITHER TMDB or IMDb
            # Pass 1: Group by TMDB (finds items with same TMDB)
            # Pass 2: Group by IMDb (finds items with same IMDb but different/missing TMDB)
            if media_type == 'show':
                cursor.execute(f"""
                    SELECT * FROM (
                        -- Pass 1: TMDB-based duplicates
                        SELECT 'tmdb_' || tmdb_id as unified_id,
                               MIN(imdb_id) as imdb_id, tmdb_id, season_number, episode_number,
                               COUNT(*) as total_versions
                        FROM media_items
                        WHERE {type_filter} AND tmdb_id IS NOT NULL
                              AND state IN ('Collected', 'Upgrading') AND ghostlisted = 0
                        GROUP BY tmdb_id, season_number, episode_number
                        HAVING total_versions > 1

                        UNION

                        -- Pass 2: IMDb-based duplicates (where TMDB differs or is missing)
                        SELECT imdb_id as unified_id,
                               imdb_id, MIN(tmdb_id) as tmdb_id, season_number, episode_number,
                               COUNT(*) as total_versions
                        FROM media_items
                        WHERE {type_filter} AND imdb_id IS NOT NULL
                              AND state IN ('Collected', 'Upgrading') AND ghostlisted = 0
                              AND imdb_id IN (
                                  SELECT imdb_id FROM media_items
                                  WHERE {type_filter} AND imdb_id IS NOT NULL
                                  GROUP BY imdb_id, season_number, episode_number
                                  HAVING COUNT(DISTINCT tmdb_id) > 1
                                     OR (COUNT(*) > COUNT(tmdb_id))
                              )
                        GROUP BY imdb_id, season_number, episode_number
                        HAVING total_versions > 1
                    )
                    ORDER BY unified_id, season_number, episode_number
                """)
            else:
                cursor.execute(f"""
                    SELECT * FROM (
                        -- Pass 1: TMDB-based duplicates
                        SELECT 'tmdb_' || tmdb_id as unified_id,
                               MIN(imdb_id) as imdb_id, tmdb_id,
                               COUNT(*) as total_versions
                        FROM media_items
                        WHERE {type_filter} AND tmdb_id IS NOT NULL
                              AND state IN ('Collected', 'Upgrading') AND ghostlisted = 0
                        GROUP BY tmdb_id
                        HAVING total_versions > 1

                        UNION

                        -- Pass 2: IMDb-based duplicates (where TMDB differs or is missing)
                        SELECT imdb_id as unified_id,
                               imdb_id, MIN(tmdb_id) as tmdb_id,
                               COUNT(*) as total_versions
                        FROM media_items
                        WHERE {type_filter} AND imdb_id IS NOT NULL
                              AND state IN ('Collected', 'Upgrading') AND ghostlisted = 0
                              AND imdb_id IN (
                                  SELECT imdb_id FROM media_items
                                  WHERE {type_filter} AND imdb_id IS NOT NULL
                                  GROUP BY imdb_id
                                  HAVING COUNT(DISTINCT tmdb_id) > 1
                                     OR (COUNT(*) > COUNT(tmdb_id))
                              )
                        GROUP BY imdb_id
                        HAVING total_versions > 1
                    )
                    ORDER BY unified_id
                """)

            problem_movies = cursor.fetchall()
            total_movies = len(problem_movies)
            total_deleted = 0
            results = []
            groups = []  # Store groups with keep/delete items
            items_to_delete_with_cleanup = []  # Collected/Upgrading items needing full cleanup
            items_to_delete_database_only = []  # Blacklisted/other items needing only database deletion
            total_excluded = 0  # Count of items excluded by patterns

            for movie_row in problem_movies:
                unified_id = movie_row[0]
                imdb_id = movie_row[1]
                tmdb_id = movie_row[2]

                # Build WHERE clause based on group type (TMDB-based or IMDb-based)
                # The UNION query already separated these into distinct groups
                if unified_id.startswith('tmdb_'):
                    # TMDB-based group: match all items with this TMDB
                    tmdb_id_value = unified_id.replace('tmdb_', '')
                    id_where = "tmdb_id = ?"
                    id_params = [tmdb_id_value]
                else:
                    # IMDb-based group: match all items with this IMDb
                    id_where = "imdb_id = ?"
                    id_params = [unified_id]

                # Get all collected versions of this item with their scores
                if media_type == 'show':
                    season_num = movie_row[3]
                    episode_num = movie_row[4]
                    cursor.execute(f"""
                        SELECT id, title, state, ghostlisted, content_source, version,
                               filled_by_file, location_basename, location_on_disk, filled_by_torrent_id,
                               season_number, episode_number, current_score
                        FROM media_items
                        WHERE {id_where} AND {type_filter}
                              AND season_number = ? AND episode_number = ?
                              AND state = 'Collected' AND ghostlisted = 0
                        ORDER BY current_score DESC NULLS LAST, id
                    """, id_params + [season_num, episode_num])

                    versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                          'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id',
                                          'season_number', 'episode_number', 'current_score'], row))
                               for row in cursor.fetchall()]
                else:
                    cursor.execute(f"""
                        SELECT id, title, state, ghostlisted, content_source, version,
                               filled_by_file, location_basename, location_on_disk, filled_by_torrent_id,
                               current_score
                        FROM media_items
                        WHERE {id_where} AND {type_filter}
                              AND state = 'Collected' AND ghostlisted = 0
                        ORDER BY current_score DESC NULLS LAST, id
                    """, id_params)

                    versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                          'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id',
                                          'current_score'], row))
                               for row in cursor.fetchall()]

                # Skip if we don't have multiple versions
                if len(versions) < 2:
                    continue

                # Apply NAS filter
                if _skip_for_nas_filter(versions):
                    continue

                # Load user's configured version settings and helper function for quality scoring
                from scraper.functions.rank_results import check_preferred

                scraping_versions = get_setting('Scraping', 'versions', {})

                # Helper function to calculate quality score by matching filename against filters
                def get_version_quality_score(item):
                    """Calculate quality score based on version settings and filename matching"""
                    # Get the version name for this item
                    version_str = item.get('version', '')
                    if not version_str:
                        return 0

                    # Remove asterisks for version matching
                    clean_version = version_str.rstrip('*').strip()

                    # Get version config (try exact match, then case-insensitive)
                    version_config = scraping_versions.get(clean_version)
                    if not version_config:
                        for ver_name, ver_config in scraping_versions.items():
                            if ver_name.lower() == clean_version.lower():
                                version_config = ver_config
                                break

                    if not version_config:
                        return 0  # Unknown version

                    # Calculate base quality score from weights and settings
                    quality_score = 0

                    # Add ALL weight fields
                    quality_score += float(version_config.get('resolution_weight', 3.0))
                    quality_score += float(version_config.get('hdr_weight', 3.0))
                    quality_score += float(version_config.get('size_weight', 3.0))
                    quality_score += float(version_config.get('bitrate_weight', 3.0))
                    quality_score += float(version_config.get('similarity_weight', 3.0))
                    quality_score += float(version_config.get('year_match_weight', 3.0))
                    quality_score += float(version_config.get('country_weight', 3.0))
                    quality_score += float(version_config.get('language_weight', 3.0))

                    # Add resolution preference
                    max_res = version_config.get('max_resolution', '1080p')
                    if max_res == '2160p':
                        quality_score += 50
                    elif max_res == '1080p':
                        quality_score += 30
                    elif max_res == '720p':
                        quality_score += 15
                    elif max_res == 'SD':
                        quality_score += 5

                    # Add HDR preference
                    if version_config.get('enable_hdr', False):
                        quality_score += 20

                    # Add quality thresholds
                    min_size = float(version_config.get('min_size_gb', 0))
                    if min_size > 0:
                        quality_score += min_size * 0.5

                    min_bitrate = float(version_config.get('min_bitrate_mbps', 0))
                    if min_bitrate > 0:
                        quality_score += min_bitrate * 0.2

                    # Get filename from item (first non-null) - fallback to empty string
                    filename = item.get('filled_by_file') or item.get('location_basename') or item.get('location_on_disk') or ''

                    if filename:
                        # Use the existing check_preferred function to match patterns
                        # Match against preferred_filter_in (bonus)
                        preferred_filter_in = version_config.get('preferred_filter_in', [])
                        if preferred_filter_in:
                            in_score, _ = check_preferred(preferred_filter_in, [filename], is_bonus=True)
                            quality_score += in_score

                        # Match against preferred_filter_out (penalty)
                        preferred_filter_out = version_config.get('preferred_filter_out', [])
                        if preferred_filter_out:
                            out_score, _ = check_preferred(preferred_filter_out, [filename], is_bonus=False)
                            quality_score += out_score  # out_score is already negative

                    # Penalty for asterisks in version string (less confident match)
                    asterisk_penalty = version_str.count('*') * 5
                    quality_score -= asterisk_penalty

                    return quality_score

                # Calculate quality scores for all items and store in dict
                for v in versions:
                    v['calculated_quality_score'] = get_version_quality_score(v)

                # Check if we have a mix of scored/unscored items
                # If ANY item has score 0.0/NULL, ignore ALL scores and use version quality ranking
                has_scored = any(v.get('current_score') and v.get('current_score') > 0 for v in versions)
                has_unscored = any(not v.get('current_score') or v.get('current_score') <= 0 for v in versions)
                use_version_quality_only = has_scored and has_unscored  # Mixed scoring

                def get_sort_key(v):
                    """Sort key: use scores if all have scores, otherwise use version quality for all"""
                    if use_version_quality_only:
                        # Mixed scoring detected - use version quality (already calculated)
                        score = v.get('current_score')
                        has_valid_score = 1 if score and score > 0 else 0
                        return (
                            v['calculated_quality_score'],  # Primary: version quality
                            has_valid_score,  # Secondary: prefer items with actual scores
                            score or 0,  # Tertiary: actual score value as tiebreaker
                            v['id']  # Final: most recent
                        )
                    else:
                        # All scored or all unscored - use appropriate method
                        score = v.get('current_score')
                        if score is None or score <= 0:
                            return (0, v['calculated_quality_score'], v['id'])
                        else:
                            return (1, score, v['id'])

                # When version_match == 'same', only compare versions with the same version field
                if version_match == 'same':
                    # Group by version field (strip asterisks for comparison)
                    version_groups = {}
                    for v in versions:
                        ver_field = v.get('version', 'Unknown')
                        # Strip asterisks to normalize: "Default" == "Default*" == "Default**"
                        clean_ver_field = ver_field.rstrip('*').strip() if ver_field else 'Unknown'
                        if clean_ver_field not in version_groups:
                            version_groups[clean_ver_field] = []
                        version_groups[clean_ver_field].append(v)

                    # Process each version group separately
                    for ver_field, ver_list in version_groups.items():
                        if len(ver_list) < 2:
                            continue  # Skip groups with only one version

                        # Sort this version group
                        ver_list.sort(key=get_sort_key, reverse=True)

                        # Keep the first, delete the rest
                        version_to_keep = ver_list[0]
                        versions_to_delete = ver_list[1:]

                        # Build group data for this version field group
                        group_keep_items = []
                        group_delete_items = []

                        # Add kept version
                        location = version_to_keep['filled_by_file'] or version_to_keep['location_basename'] or version_to_keep['location_on_disk'] or 'N/A'
                        keep_data = {
                            'id': version_to_keep['id'],
                            'title': version_to_keep['title'],
                            'state': version_to_keep['state'],
                            'ghostlisted': version_to_keep.get('ghostlisted', 0),
                            'version': version_to_keep['version'],
                            'content_source': version_to_keep['content_source'],
                            'location': location,
                            'torrent_id': version_to_keep['filled_by_torrent_id'] or 'N/A',
                            'current_score': version_to_keep.get('calculated_quality_score', version_to_keep.get('current_score', 'N/A'))
                        }
                        if media_type == 'show':
                            keep_data['season_number'] = version_to_keep.get('season_number', 'N/A')
                            keep_data['episode_number'] = version_to_keep.get('episode_number', 'N/A')
                        group_keep_items.append(keep_data)

                        # Collect items for deletion (check exclusions first)
                        for version in versions_to_delete:
                            location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'

                            # Check if item should be excluded from deletion
                            is_excluded, matched_pattern = should_exclude_item(version, exclude_patterns)

                            if is_excluded:
                                # Item is excluded - mark it but don't delete
                                total_excluded += 1
                                delete_data = {
                                    'id': version['id'],
                                    'title': version['title'],
                                    'state': version['state'],
                                    'ghostlisted': version.get('ghostlisted', 0),
                                    'version': version['version'],
                                    'content_source': version['content_source'],
                                    'location': location,
                                    'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                                    'action': 'excluded',
                                    'excluded_pattern': matched_pattern,
                                    'current_score': version.get('calculated_quality_score', version.get('current_score', 'N/A'))
                                }
                                if media_type == 'show':
                                    delete_data['season_number'] = version.get('season_number', 'N/A')
                                    delete_data['episode_number'] = version.get('episode_number', 'N/A')
                                group_delete_items.append(delete_data)
                                results.append(delete_data)
                            else:
                                # Item will be deleted - separate by state for proper cleanup
                                if version['state'] in ('Collected', 'Upgrading'):
                                    items_to_delete_with_cleanup.append(version['id'])
                                else:
                                    items_to_delete_database_only.append(version['id'])

                                total_deleted += 1
                                delete_data = {
                                    'id': version['id'],
                                    'title': version['title'],
                                    'state': version['state'],
                                    'ghostlisted': version.get('ghostlisted', 0),
                                    'version': version['version'],
                                    'content_source': version['content_source'],
                                    'location': location,
                                    'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                                    'action': 'deleted' if not dry_run else 'would_delete',
                                    'current_score': version.get('calculated_quality_score', version.get('current_score', 'N/A'))
                                }
                                if media_type == 'show':
                                    delete_data['season_number'] = version.get('season_number', 'N/A')
                                    delete_data['episode_number'] = version.get('episode_number', 'N/A')
                                group_delete_items.append(delete_data)
                                results.append(delete_data)

                        # Add this group
                        group_data = {
                            'keep_items': group_keep_items,
                            'delete_items': group_delete_items
                        }
                        groups.append(group_data)

                    # Skip the rest of the loop since we've processed all version groups
                    continue

                # version_match == 'all': Compare across all version fields
                # Sort by: (has_valid_score, score_or_version_quality, id)
                versions.sort(key=get_sort_key, reverse=True)

                # Keep the first (best) version, delete the rest
                version_to_keep = versions[0]
                versions_to_delete = versions[1:]

                # Build group data
                group_keep_items = []
                group_delete_items = []

                # Add kept version
                location = version_to_keep['filled_by_file'] or version_to_keep['location_basename'] or version_to_keep['location_on_disk'] or 'N/A'
                keep_data = {
                    'id': version_to_keep['id'],
                    'title': version_to_keep['title'],
                    'state': version_to_keep['state'],
                    'ghostlisted': version_to_keep.get('ghostlisted', 0),
                    'version': version_to_keep['version'],
                    'content_source': version_to_keep['content_source'],
                    'location': location,
                    'torrent_id': version_to_keep['filled_by_torrent_id'] or 'N/A',
                    'current_score': version_to_keep.get('calculated_quality_score', version_to_keep.get('current_score', 'N/A'))
                }
                if media_type == 'show':
                    keep_data['season_number'] = version_to_keep.get('season_number', 'N/A')
                    keep_data['episode_number'] = version_to_keep.get('episode_number', 'N/A')
                group_keep_items.append(keep_data)

                # Collect items for deletion (check exclusions first)
                for version in versions_to_delete:
                    location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'

                    # Check if item should be excluded from deletion
                    is_excluded, matched_pattern = should_exclude_item(version, exclude_patterns)

                    if is_excluded:
                        # Item is excluded - mark it but don't delete
                        total_excluded += 1
                        delete_data = {
                            'id': version['id'],
                            'title': version['title'],
                            'state': version['state'],
                            'ghostlisted': version.get('ghostlisted', 0),
                            'version': version['version'],
                            'content_source': version['content_source'],
                            'location': location,
                            'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                            'action': 'excluded',
                            'excluded_pattern': matched_pattern,
                            'current_score': version.get('calculated_quality_score', version.get('current_score', 'N/A'))
                        }
                        if media_type == 'show':
                            delete_data['season_number'] = version.get('season_number', 'N/A')
                            delete_data['episode_number'] = version.get('episode_number', 'N/A')
                        group_delete_items.append(delete_data)
                        results.append(delete_data)
                    else:
                        # Item will be deleted - separate by state for proper cleanup
                        if version['state'] in ('Collected', 'Upgrading'):
                            items_to_delete_with_cleanup.append(version['id'])
                        else:
                            items_to_delete_database_only.append(version['id'])

                        total_deleted += 1
                        delete_data = {
                            'id': version['id'],
                            'title': version['title'],
                            'state': version['state'],
                            'ghostlisted': version.get('ghostlisted', 0),
                            'version': version['version'],
                            'content_source': version['content_source'],
                            'location': location,
                            'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                            'action': 'deleted' if not dry_run else 'would_delete',
                            'current_score': version.get('calculated_quality_score', version.get('current_score', 'N/A'))
                        }
                        if media_type == 'show':
                            delete_data['season_number'] = version.get('season_number', 'N/A')
                            delete_data['episode_number'] = version.get('episode_number', 'N/A')
                        group_delete_items.append(delete_data)
                        results.append(delete_data)

                # Add this group
                group_data = {
                    'keep_items': group_keep_items,
                    'delete_items': group_delete_items
                }
                groups.append(group_data)

            # Perform actual deletions after collecting all items
            if not dry_run and (items_to_delete_with_cleanup or items_to_delete_database_only):
                # Pause queue for cleanup operations (not for dry run)
                needs_pause = (
                    len(items_to_delete_with_cleanup) > 5 or  # Large batch
                    len(items_to_delete_with_cleanup) > 0  # Any items with physical files
                )

                if needs_pause:
                    if program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
                        if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                            program_runner.pause_info = {
                                "reason_string": "Duplicate version cleanup in progress",
                                "error_type": "SYSTEM_MAINTENANCE",
                                "service_name": "Manage Duplicates",
                                "status_code": None,
                                "retry_count": 0
                            }
                            program_runner.pause_queue()
                            paused_queue = True
                            logging.info(f"[CLEANUP_DUPLICATES] Queue paused for {len(items_to_delete_with_cleanup)} collected item deletion")

                # Delete Collected/Upgrading items with full cleanup (bypass ghostlist mode)
                if items_to_delete_with_cleanup:
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} Collected/Upgrading items with full cleanup (bypassing ghostlist mode)")

                    # Get full item details for Plex deletion
                    from database.database_reading import get_items_by_ids
                    from utilities.plex_functions import remove_file_from_plex

                    items_to_delete_details = get_items_by_ids(items_to_delete_with_cleanup)

                    # Track deletion progress
                    deletion_start_time = time.time()
                    total_items = len(items_to_delete_with_cleanup)
                    items_processed = 0
                    plex_deleted = 0
                    plex_failed = 0
                    plex_not_found = 0
                    items_with_torrent_id = 0
                    items_without_torrent_id = 0

                    # Generate session ID for progress tracking
                    import uuid
                    session_id = str(uuid.uuid4())
                    cleanup_progress[session_id] = {
                        'status': 'running',
                        'phase': 'plex_deletion',
                        'items_processed': 0,
                        'total_items': total_items,
                        'elapsed': 0,
                        'eta': 0,
                        'session_id': session_id
                    }

                    logging.info(f"[CLEANUP_DUPLICATES] Starting Plex deletion for {total_items} items (session: {session_id})")

                    # PHASE 1: Delete items from Plex (Plex handles filesystem cleanup automatically)
                    for item in items_to_delete_details:
                        items_processed += 1

                        # Update progress tracking
                        elapsed = time.time() - deletion_start_time
                        avg_time = elapsed / items_processed if items_processed > 0 else 0
                        eta = int(avg_time * (total_items - items_processed))
                        cleanup_progress[session_id] = {
                            'status': 'running',
                            'phase': 'plex_deletion',
                            'items_processed': items_processed,
                            'total_items': total_items,
                            'plex_deleted': plex_deleted,
                            'plex_failed': plex_failed,
                            'plex_not_found': plex_not_found,
                            'elapsed': int(elapsed),
                            'eta': eta,
                            'session_id': session_id
                        }
                        item_id = item['id']
                        item_title = item.get('title', 'Unknown')
                        item_type = item.get('type', 'movie')
                        location = item.get('location_on_disk') or item.get('filled_by_file')
                        episode_title = None

                        # For shows, include episode title
                        if item_type == 'episode':
                            season_num = item.get('season_number')
                            episode_num = item.get('episode_number')
                            if season_num and episode_num:
                                episode_title = f"S{season_num:02d}E{episode_num:02d}"

                        # Track torrent ID stats
                        if item.get('filled_by_torrent_id'):
                            items_with_torrent_id += 1
                        else:
                            items_without_torrent_id += 1

                        # Calculate progress and ETA
                        if items_processed > 1:
                            elapsed = time.time() - deletion_start_time
                            avg_time_per_item = elapsed / items_processed
                            items_remaining = total_items - items_processed
                            estimated_remaining_seconds = int(avg_time_per_item * items_remaining)

                            # Log every 10 items for better visibility
                            if items_processed % 10 == 0:
                                logging.info(f"[CLEANUP_DUPLICATES] Progress: {items_processed}/{total_items} items ({items_processed/total_items*100:.1f}%), ETA: {estimated_remaining_seconds}s, avg: {avg_time_per_item:.2f}s/item")

                        # Delete from Plex using API (Plex handles filesystem cleanup)
                        if location:
                            try:
                                result = remove_file_from_plex(
                                    item_title=item_title,
                                    item_path=location,
                                    episode_title=episode_title
                                )
                                if result:
                                    plex_deleted += 1
                                else:
                                    plex_not_found += 1
                            except Exception as e:
                                plex_failed += 1
                                logging.warning(f"[CLEANUP_DUPLICATES] Failed to delete from Plex for item {item_id} ({item_title}): {e}")
                        else:
                            plex_not_found += 1

                    logging.info(f"[CLEANUP_DUPLICATES] Plex deletion complete: {plex_deleted} deleted, {plex_failed} failed, {plex_not_found} not found")
                    logging.info(f"[CLEANUP_DUPLICATES] Torrent ID stats: {items_with_torrent_id} with torrent_id, {items_without_torrent_id} without torrent_id")

                    # PHASE 2: Call DeletionManager for debrid/cache/symlinks cleanup
                    # (Plex deletion already done above - Plex handles filesystem cleanup)
                    logging.info(f"[CLEANUP_DUPLICATES] Running DeletionManager for debrid/cache/symlinks cleanup")
                    debrid_provider = get_debrid_provider()
                    deletion_manager = DeletionManager(debrid_provider=debrid_provider)

                    deletion_result = deletion_manager.delete_multiple_items(
                        item_ids=items_to_delete_with_cleanup,
                        blacklist=False,
                        blacklist_sources=False,
                        delete_from_media_server=False,  # Already handled above
                        delete_files=False,  # Already handled above
                        delete_from_debrid=True,
                        delete_symlinks=True,
                        clear_cache=True,
                        remove_from_content_source=False,
                        skip_database=True,  # Skip DeletionManager's database operation (bypasses ghostlist mode)
                        force_delete_parent_folder=False,
                        plex_deletion_type=None
                    )

                    if not deletion_result['success']:
                        logging.error(f"[CLEANUP_DUPLICATES] Deletion errors: {deletion_result.get('errors', [])}")
                        if deletion_result.get('database_locked'):
                            logging.error("[CLEANUP_DUPLICATES] Database lock detected during deletion")

                    # Log debrid removal stats
                    debrid_removed = deletion_result.get('debrid_torrents_removed', 0)
                    if debrid_removed > 0:
                        logging.info(f"[CLEANUP_DUPLICATES] Debrid removal: {debrid_removed} torrents removed")

                    # PHASE 4: Delete from database (bypasses ghostlist mode)
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} items from database (force delete)")
                    placeholders = ','.join(['?'] * len(items_to_delete_with_cleanup))
                    cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_with_cleanup)

                # Delete Blacklisted/other items (database only - no files to clean up)
                if items_to_delete_database_only:
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_database_only)} Blacklisted/other items (database only)")
                    placeholders = ','.join(['?'] * len(items_to_delete_database_only))
                    cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_database_only)

                conn.commit()
            elif not dry_run:
                conn.commit()

            elapsed_time = time.time() - start_time

            # Mark progress as complete
            if 'session_id' in locals() and session_id in cleanup_progress:
                cleanup_progress[session_id]['status'] = 'complete'
                cleanup_progress[session_id]['elapsed'] = int(elapsed_time)

            # Build message with excluded count if applicable
            message = f'{"Would delete" if dry_run else "Deleted"} {total_deleted} lower-quality duplicates from {total_movies} {media_label} (kept best quality)'
            if total_excluded > 0:
                message += f', excluded {total_excluded} items by pattern'

            return {
                'success': True,
                'message': message,
                'total_movies': total_movies,
                'total_deleted': total_deleted,
                'total_excluded': total_excluded,
                'elapsed_time': round(elapsed_time, 2),
                'dry_run': dry_run,
                'keep_action': keep_action,
                'version_match': version_match,
                'results': results,
                'groups': groups,
                'session_id': session_id if 'session_id' in locals() else None
            }, 200

        # Handle keep_collected_delete_ghostlisted and keep_ghostlisted_delete_collected modes
        if keep_action in ('keep_collected_delete_ghostlisted', 'keep_ghostlisted_delete_collected'):
            # Find items with both collected and ghostlisted versions
            # For shows, group by season/episode to find actual duplicate episodes
            if media_type == 'show':
                cursor.execute(f"""
                    SELECT imdb_id, season_number, episode_number,
                           COUNT(*) as total_versions,
                           SUM(CASE WHEN ghostlisted = 1 THEN 1 ELSE 0 END) as ghostlisted_count,
                           SUM(CASE WHEN state IN ('Collected', 'Upgrading') THEN 1 ELSE 0 END) as collected_count
                    FROM media_items
                    WHERE {type_filter} AND imdb_id IS NOT NULL
                    GROUP BY imdb_id, season_number, episode_number
                    HAVING ghostlisted_count > 0 AND collected_count > 0
                """)
            else:
                cursor.execute(f"""
                    SELECT imdb_id,
                           COUNT(*) as total_versions,
                           SUM(CASE WHEN ghostlisted = 1 THEN 1 ELSE 0 END) as ghostlisted_count,
                           SUM(CASE WHEN state IN ('Collected', 'Upgrading') THEN 1 ELSE 0 END) as collected_count
                    FROM media_items
                    WHERE {type_filter} AND imdb_id IS NOT NULL
                    GROUP BY imdb_id
                    HAVING ghostlisted_count > 0 AND collected_count > 0
                """)

            problem_movies = cursor.fetchall()
            total_movies = len(problem_movies)
            total_deleted = 0
            results = []
            groups = []  # Store groups with keep/delete items
            items_to_delete_with_cleanup = []  # Collected/Upgrading items needing full cleanup
            items_to_delete_database_only = []  # Blacklisted/other items needing only database deletion
            total_excluded = 0  # Count of items excluded by patterns

            for movie_row in problem_movies:
                imdb_id = movie_row[0]

                # Get all versions of this item
                # For shows, also filter by season/episode to get the correct duplicates
                if media_type == 'show':
                    season_num = movie_row[1]
                    episode_num = movie_row[2]
                    cursor.execute(f"""
                        SELECT id, title, state, ghostlisted, content_source, version,
                               filled_by_file, location_basename, location_on_disk, filled_by_torrent_id,
                               season_number, episode_number
                        FROM media_items
                        WHERE imdb_id = ? AND {type_filter}
                              AND season_number = ? AND episode_number = ?
                        ORDER BY state, id
                    """, (imdb_id, season_num, episode_num))

                    versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                          'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id',
                                          'season_number', 'episode_number'], row))
                               for row in cursor.fetchall()]
                else:
                    cursor.execute(f"""
                        SELECT id, title, state, ghostlisted, content_source, version,
                               filled_by_file, location_basename, location_on_disk, filled_by_torrent_id
                        FROM media_items
                        WHERE imdb_id = ? AND {type_filter}
                        ORDER BY state, id
                    """, (imdb_id,))

                    versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                          'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id'], row))
                               for row in cursor.fetchall()]

                # Separate collected and ghostlisted versions
                collected_versions = [v for v in versions if v['state'] in ('Collected', 'Upgrading')]
                ghostlisted_versions = [v for v in versions if v['ghostlisted'] == 1]

                # Safety check: ensure we have both types before proceeding
                if not collected_versions or not ghostlisted_versions:
                    logging.warning(f"Skipping {imdb_id} - missing collected or ghostlisted versions (safety check)")
                    continue

                # Apply NAS filter
                if _skip_for_nas_filter(versions):
                    continue

                # Determine which versions to delete based on keep_action
                versions_to_delete = []
                versions_to_keep = []

                if keep_action == 'keep_ghostlisted_delete_collected':
                    # Delete collected versions, keep ghostlisted
                    if version_match == 'same':
                        # Only delete collected versions that match a ghostlisted version
                        ghostlisted_version_set = {v['version'] for v in ghostlisted_versions}
                        versions_to_delete = [v for v in collected_versions if v['version'] in ghostlisted_version_set]
                        versions_to_keep = [v for v in ghostlisted_versions if v['version'] in ghostlisted_version_set]
                    else:  # 'all'
                        # Delete all collected versions regardless of version field
                        versions_to_delete = collected_versions
                        versions_to_keep = ghostlisted_versions
                else:  # keep_collected_delete_ghostlisted
                    # Delete ghostlisted versions, keep collected (default behavior)
                    if version_match == 'same':
                        # Only delete ghostlisted versions that match a collected version
                        collected_version_set = {v['version'] for v in collected_versions}
                        versions_to_delete = [v for v in ghostlisted_versions if v['version'] in collected_version_set]
                        versions_to_keep = [v for v in collected_versions if v['version'] in collected_version_set]
                    else:  # 'all'
                        # Delete all ghostlisted versions regardless of version field
                        versions_to_delete = ghostlisted_versions
                        versions_to_keep = collected_versions

                # Build group data
                group_keep_items = []
                group_delete_items = []

                # Add kept versions
                for version in versions_to_keep:
                    location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'
                    keep_data = {
                        'id': version['id'],
                        'title': version['title'],
                        'state': version['state'],
                        'ghostlisted': version['ghostlisted'],
                        'version': version['version'],
                        'content_source': version['content_source'],
                        'location': location,
                        'torrent_id': version['filled_by_torrent_id'] or 'N/A'
                    }
                    if media_type == 'show':
                        keep_data['season_number'] = version.get('season_number', 'N/A')
                        keep_data['episode_number'] = version.get('episode_number', 'N/A')
                    group_keep_items.append(keep_data)

                # Collect items for deletion (check exclusions first)
                for version in versions_to_delete:
                    location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'

                    # Check if item should be excluded from deletion
                    is_excluded, matched_pattern = should_exclude_item(version, exclude_patterns)

                    if is_excluded:
                        # Item is excluded - mark it but don't delete
                        total_excluded += 1
                        delete_data = {
                            'id': version['id'],
                            'title': version['title'],
                            'state': version['state'],
                            'ghostlisted': version['ghostlisted'],
                            'version': version['version'],
                            'content_source': version['content_source'],
                            'location': location,
                            'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                            'action': 'excluded',
                            'excluded_pattern': matched_pattern
                        }
                        if media_type == 'show':
                            delete_data['season_number'] = version.get('season_number', 'N/A')
                            delete_data['episode_number'] = version.get('episode_number', 'N/A')
                        group_delete_items.append(delete_data)
                        results.append(delete_data)
                    else:
                        # Item will be deleted - separate by state for proper cleanup
                        if version['state'] in ('Collected', 'Upgrading'):
                            items_to_delete_with_cleanup.append(version['id'])
                        else:
                            items_to_delete_database_only.append(version['id'])

                        total_deleted += 1
                        delete_data = {
                            'id': version['id'],
                            'title': version['title'],
                            'state': version['state'],
                            'ghostlisted': version['ghostlisted'],
                            'version': version['version'],
                            'content_source': version['content_source'],
                            'location': location,
                            'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                            'action': 'deleted' if not dry_run else 'would_delete'
                        }
                        if media_type == 'show':
                            delete_data['season_number'] = version.get('season_number', 'N/A')
                            delete_data['episode_number'] = version.get('episode_number', 'N/A')
                        group_delete_items.append(delete_data)
                        results.append(delete_data)

                # Add this group
                group_data = {
                    'keep_items': group_keep_items,
                    'delete_items': group_delete_items
                }
                groups.append(group_data)

            # Pause queues if we're performing actual deletions
            if not dry_run and (items_to_delete_with_cleanup or items_to_delete_database_only):
                # Pause queue for cleanup operations (not for dry run)
                needs_pause = (
                    len(items_to_delete_with_cleanup) > 5 or  # Large batch
                    len(items_to_delete_with_cleanup) > 0  # Any items with physical files
                )

                if needs_pause:
                    if program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
                        if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                            program_runner.pause_info = {
                                "reason_string": "Ghostlisted duplicate cleanup in progress",
                                "error_type": "SYSTEM_MAINTENANCE",
                                "service_name": "Manage Duplicates",
                                "status_code": None,
                                "retry_count": 0
                            }
                            program_runner.pause_queue()
                            paused_queue = True
                            logging.info(f"[CLEANUP_DUPLICATES] Queue paused for {len(items_to_delete_with_cleanup)} collected item deletion")

            # Perform actual deletion if not dry run
            if not dry_run and (items_to_delete_with_cleanup or items_to_delete_database_only):
                # Delete Collected/Upgrading items (full cleanup)
                if items_to_delete_with_cleanup:
                    # PHASE 1: Delete from Plex first
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} items from Plex")
                    plex_deleted = 0
                    plex_failed = 0
                    plex_not_found = 0
                    items_with_torrent_id = 0
                    items_without_torrent_id = 0

                    for item_id in items_to_delete_with_cleanup:
                        cursor.execute("""
                            SELECT title, episode_title, location_on_disk, filled_by_torrent_id
                            FROM media_items WHERE id = ?
                        """, (item_id,))
                        row = cursor.fetchone()

                        if row:
                            item_title = row[0]
                            episode_title = row[1]
                            location = row[2]
                            torrent_id = row[3]

                            # Track torrent_id stats
                            if torrent_id:
                                items_with_torrent_id += 1
                            else:
                                items_without_torrent_id += 1

                        if location:
                            try:
                                result = remove_file_from_plex(
                                    item_title=item_title,
                                    item_path=location,
                                    episode_title=episode_title
                                )
                                if result:
                                    plex_deleted += 1
                                else:
                                    plex_not_found += 1
                            except Exception as e:
                                plex_failed += 1
                                logging.warning(f"[CLEANUP_DUPLICATES] Failed to delete from Plex for item {item_id} ({item_title}): {e}")
                        else:
                            plex_not_found += 1

                    logging.info(f"[CLEANUP_DUPLICATES] Plex deletion complete: {plex_deleted} deleted, {plex_failed} failed, {plex_not_found} not found")
                    logging.info(f"[CLEANUP_DUPLICATES] Torrent ID stats: {items_with_torrent_id} with torrent_id, {items_without_torrent_id} without torrent_id")

                    # PHASE 2: Call DeletionManager for debrid/cache/symlinks cleanup
                    # (Plex deletion already done above - Plex handles filesystem cleanup)
                    logging.info(f"[CLEANUP_DUPLICATES] Running DeletionManager for debrid/cache/symlinks cleanup")
                    debrid_provider = get_debrid_provider()
                    deletion_manager = DeletionManager(debrid_provider=debrid_provider)

                    deletion_result = deletion_manager.delete_multiple_items(
                        item_ids=items_to_delete_with_cleanup,
                        blacklist=False,
                        blacklist_sources=False,
                        delete_from_media_server=False,  # Already handled above
                        delete_files=False,  # Already handled above
                        delete_from_debrid=True,
                        delete_symlinks=True,
                        clear_cache=True,
                        remove_from_content_source=False,
                        skip_database=True,  # Skip DeletionManager's database operation (bypasses ghostlist mode)
                        force_delete_parent_folder=False,
                        plex_deletion_type=None
                    )

                    if not deletion_result['success']:
                        logging.error(f"[CLEANUP_DUPLICATES] Deletion errors: {deletion_result.get('errors', [])}")
                        if deletion_result.get('database_locked'):
                            logging.error("[CLEANUP_DUPLICATES] Database lock detected during deletion")

                    # Log debrid removal stats
                    debrid_removed = deletion_result.get('debrid_torrents_removed', 0)
                    if debrid_removed > 0:
                        logging.info(f"[CLEANUP_DUPLICATES] Debrid removal: {debrid_removed} torrents removed")

                    # PHASE 4: Delete from database (bypasses ghostlist mode)
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} items from database (force delete)")
                    placeholders = ','.join(['?'] * len(items_to_delete_with_cleanup))
                    cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_with_cleanup)

                # Delete Blacklisted/other items (database only - no files to clean up)
                if items_to_delete_database_only:
                    logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_database_only)} Blacklisted/other items (database only)")
                    placeholders = ','.join(['?'] * len(items_to_delete_database_only))
                    cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_database_only)

                conn.commit()
            elif not dry_run:
                conn.commit()

            elapsed_time = time.time() - start_time

            # Build message with excluded count if applicable
            action_label = 'collected' if keep_action == 'keep_collected_delete_ghostlisted' else 'ghostlisted'
            message = f'{"Would delete" if dry_run else "Deleted"} {total_deleted} {action_label} duplicates from {total_movies} {media_label}'
            if total_excluded > 0:
                message += f', excluded {total_excluded} items by pattern'

            return {
                'success': True,
                'message': message,
                'total_movies': total_movies,
                'total_deleted': total_deleted,
                'total_excluded': total_excluded,
                'elapsed_time': round(elapsed_time, 2),
                'dry_run': dry_run,
                'keep_action': keep_action,
                'version_match': version_match,
                'results': results,
                'groups': groups
            }, 200

        # Handle keep_collected_delete_stale — delete Wanted/Scraping/Adding/Checking/Upgrading
        # duplicates that exist alongside a Collected version
        if keep_action == 'keep_collected_delete_stale':
            STALE_STATES = ('Wanted', 'Scraping', 'Adding', 'Checking', 'Unreleased', 'Final Scrape')
            stale_in = ','.join(f"'{s}'" for s in STALE_STATES)
            if media_type == 'show':
                cursor.execute(f"""
                    SELECT imdb_id, season_number, episode_number,
                           COUNT(*) as total_versions,
                           SUM(CASE WHEN state IN ({stale_in}) THEN 1 ELSE 0 END) as stale_count,
                           SUM(CASE WHEN state IN ('Collected', 'Upgrading') AND ghostlisted = 0 THEN 1 ELSE 0 END) as collected_count
                    FROM media_items
                    WHERE {type_filter} AND imdb_id IS NOT NULL
                    GROUP BY imdb_id, season_number, episode_number
                    HAVING stale_count > 0 AND collected_count > 0
                """)
            else:
                cursor.execute(f"""
                    SELECT imdb_id,
                           COUNT(*) as total_versions,
                           SUM(CASE WHEN state IN ({stale_in}) THEN 1 ELSE 0 END) as stale_count,
                           SUM(CASE WHEN state IN ('Collected', 'Upgrading') AND ghostlisted = 0 THEN 1 ELSE 0 END) as collected_count
                    FROM media_items
                    WHERE {type_filter} AND imdb_id IS NOT NULL
                    GROUP BY imdb_id
                    HAVING stale_count > 0 AND collected_count > 0
                """)

            problem_items = cursor.fetchall()
            total_movies = len(problem_items)
            total_deleted = 0
            total_excluded = 0
            results = []
            groups = []
            start_time = time.time()

            import os as _os_stale
            for row in problem_items:
                row = dict(row)
                imdb_id = row['imdb_id']
                if media_type == 'show':
                    cursor.execute(f"""
                        SELECT id, title, state, version, filled_by_file, location_on_disk,
                               season_number, episode_number, ghostlisted
                        FROM media_items
                        WHERE {type_filter} AND imdb_id = ?
                        AND season_number = ? AND episode_number = ?
                    """, (imdb_id, row['season_number'], row['episode_number']))
                else:
                    cursor.execute(f"""
                        SELECT id, title, state, version, filled_by_file, location_on_disk,
                               ghostlisted
                        FROM media_items
                        WHERE {type_filter} AND imdb_id = ?
                    """, (imdb_id,))

                all_versions = []
                for r in cursor.fetchall():
                    v = dict(r)
                    v['location_basename'] = _os_stale.path.basename(v.get('location_on_disk') or '')
                    all_versions.append(v)
                collected = [v for v in all_versions if v['state'] in ('Collected', 'Upgrading') and not v['ghostlisted']]
                stale = [v for v in all_versions if v['state'] in STALE_STATES]

                if not collected or not stale:
                    continue

                if _skip_for_nas_filter(collected):
                    total_excluded += 1
                    continue

                def _fmt_item(v, action='keep'):
                    loc = v.get('filled_by_file') or v.get('location_basename') or v.get('location_on_disk') or 'N/A'
                    return {
                        'id': v['id'],
                        'title': v.get('title', ''),
                        'state': v.get('state', ''),
                        'version': v.get('version', ''),
                        'location': loc,
                        'ghostlisted': bool(v.get('ghostlisted', 0)),
                        'current_score': v.get('current_score', 'N/A'),
                        'season_number': v.get('season_number'),
                        'episode_number': v.get('episode_number'),
                        'action': action,
                    }

                # Apply exclude patterns — protect matching stale items from deletion
                stale_to_delete = []
                stale_protected = []
                for v in stale:
                    is_excl, _ = should_exclude_item(v, exclude_patterns)
                    if is_excl:
                        stale_protected.append(v)
                    else:
                        stale_to_delete.append(v)

                group_keep_items = [_fmt_item(v, 'keep') for v in collected] + [_fmt_item(v, 'exclude') for v in stale_protected]
                group_delete_items = [_fmt_item(v, 'delete') for v in stale_to_delete]

                if not group_delete_items:
                    continue

                for v in stale_to_delete:
                    if not dry_run:
                        cursor.execute("DELETE FROM media_items WHERE id = ?", (v['id'],))
                    total_deleted += 1

                groups.append({'keep_items': group_keep_items, 'delete_items': group_delete_items})
                results.append({
                    'title': all_versions[0]['title'] if all_versions else imdb_id,
                    'imdb_id': imdb_id,
                    'keep_count': len(group_keep_items),
                    'delete_count': len(group_delete_items),
                    'kept': [v['state'] + ' v' + str(v['version']) for v in group_keep_items],
                    'deleted': [v['state'] + ' v' + str(v['version']) for v in group_delete_items],
                })

            if not dry_run:
                conn.commit()

            elapsed_time = time.time() - start_time
            message = f'{"Would delete" if dry_run else "Deleted"} {total_deleted} stale duplicates from {total_movies} {media_label} (kept Collected)'
            if total_excluded:
                message += f', excluded {total_excluded} items by NAS filter'

            return {
                'success': True,
                'message': message,
                'total_movies': total_movies,
                'total_deleted': total_deleted,
                'total_excluded': total_excluded,
                'elapsed_time': round(elapsed_time, 2),
                'dry_run': dry_run,
                'keep_action': keep_action,
                'version_match': version_match,
                'results': results,
                'groups': groups
            }, 200

        # Find items with both collected and blacklisted (non-ghostlisted) versions
        # For shows, group by season/episode to find actual duplicate episodes
        if media_type == 'show':
            cursor.execute(f"""
                SELECT imdb_id, season_number, episode_number,
                       COUNT(*) as total_versions,
                       SUM(CASE WHEN state = 'Blacklisted' AND ghostlisted = 0 THEN 1 ELSE 0 END) as blacklisted_count,
                       SUM(CASE WHEN state = 'Collected' THEN 1 ELSE 0 END) as collected_count
                FROM media_items
                WHERE {type_filter} AND imdb_id IS NOT NULL
                GROUP BY imdb_id, season_number, episode_number
                HAVING blacklisted_count > 0 AND collected_count > 0
            """)
        else:
            cursor.execute(f"""
                SELECT imdb_id,
                       COUNT(*) as total_versions,
                       SUM(CASE WHEN state = 'Blacklisted' AND ghostlisted = 0 THEN 1 ELSE 0 END) as blacklisted_count,
                       SUM(CASE WHEN state = 'Collected' THEN 1 ELSE 0 END) as collected_count
                FROM media_items
                WHERE {type_filter} AND imdb_id IS NOT NULL
                GROUP BY imdb_id
                HAVING blacklisted_count > 0 AND collected_count > 0
            """)

        problem_movies = cursor.fetchall()
        total_movies = len(problem_movies)
        total_deleted = 0
        results = []
        groups = []  # Store groups with keep/delete items
        items_to_delete_with_cleanup = []  # Collected/Upgrading items needing full cleanup
        items_to_delete_database_only = []  # Blacklisted/other items needing only database deletion
        total_excluded = 0  # Count of items excluded by patterns

        for movie_row in problem_movies:
            imdb_id = movie_row[0]

            # Get all versions of this item
            # For shows, also filter by season/episode to get the correct duplicates
            if media_type == 'show':
                season_num = movie_row[1]
                episode_num = movie_row[2]
                cursor.execute(f"""
                    SELECT id, title, state, ghostlisted, content_source, version,
                           filled_by_file, location_basename, location_on_disk, filled_by_torrent_id,
                           season_number, episode_number
                    FROM media_items
                    WHERE imdb_id = ? AND {type_filter}
                          AND season_number = ? AND episode_number = ?
                    ORDER BY state, id
                """, (imdb_id, season_num, episode_num))

                versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                      'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id',
                                      'season_number', 'episode_number'], row))
                           for row in cursor.fetchall()]
            else:
                cursor.execute(f"""
                    SELECT id, title, state, ghostlisted, content_source, version,
                           filled_by_file, location_basename, location_on_disk, filled_by_torrent_id
                    FROM media_items
                    WHERE imdb_id = ? AND {type_filter}
                    ORDER BY state, id
                """, (imdb_id,))

                versions = [dict(zip(['id', 'title', 'state', 'ghostlisted', 'content_source', 'version',
                                      'filled_by_file', 'location_basename', 'location_on_disk', 'filled_by_torrent_id'], row))
                           for row in cursor.fetchall()]

            # Separate collected and blacklisted (non-ghostlisted) versions
            collected_versions = [v for v in versions if v['state'] in ('Collected', 'Upgrading')]
            blacklisted_versions = [v for v in versions if v['state'] == 'Blacklisted' and v['ghostlisted'] == 0]

            # Safety check: ensure we have both types before proceeding
            if not collected_versions or not blacklisted_versions:
                logging.warning(f"Skipping {imdb_id} - missing collected or blacklisted versions (safety check)")
                continue

            # Apply NAS filter
            if _skip_for_nas_filter(versions):
                continue

            # Determine which versions to delete based on keep_action
            versions_to_delete = []
            versions_to_keep = []

            if keep_action == 'keep_blacklisted':
                # Delete collected versions, keep blacklisted
                if version_match == 'same':
                    # Only delete collected versions that match a blacklisted version
                    blacklisted_version_set = {v['version'] for v in blacklisted_versions}
                    versions_to_delete = [v for v in collected_versions if v['version'] in blacklisted_version_set]
                    versions_to_keep = [v for v in blacklisted_versions if v['version'] in blacklisted_version_set]
                else:  # 'all'
                    # Delete all collected versions regardless of version field
                    versions_to_delete = collected_versions
                    versions_to_keep = blacklisted_versions
            else:  # keep_collected
                # Delete blacklisted versions, keep collected (default behavior)
                if version_match == 'same':
                    # Only delete blacklisted versions that match a collected version
                    collected_version_set = {v['version'] for v in collected_versions}
                    versions_to_delete = [v for v in blacklisted_versions if v['version'] in collected_version_set]
                    versions_to_keep = [v for v in collected_versions if v['version'] in collected_version_set]
                else:  # 'all'
                    # Delete all blacklisted versions regardless of version field
                    versions_to_delete = blacklisted_versions
                    versions_to_keep = collected_versions

            # Build group data
            group_keep_items = []
            group_delete_items = []

            # Process keep items
            for version in versions_to_keep:
                location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'
                keep_data = {
                    'id': version['id'],
                    'title': version['title'],
                    'state': version['state'],
                    'ghostlisted': version['ghostlisted'],
                    'version': version['version'],
                    'content_source': version['content_source'],
                    'location': location,
                    'torrent_id': version['filled_by_torrent_id'] or 'N/A'
                }
                if media_type == 'show':
                    keep_data['season_number'] = version['season_number'] if 'season_number' in version.keys() else 'N/A'
                    keep_data['episode_number'] = version['episode_number'] if 'episode_number' in version.keys() else 'N/A'
                group_keep_items.append(keep_data)

            # Collect items for deletion (check exclusions first)
            for version in versions_to_delete:
                # Determine which location field to use (first non-null)
                location = version['filled_by_file'] or version['location_basename'] or version['location_on_disk'] or 'N/A'

                # Check if item should be excluded from deletion
                is_excluded, matched_pattern = should_exclude_item(version, exclude_patterns)

                if is_excluded:
                    # Item is excluded - mark it but don't delete
                    total_excluded += 1
                    delete_data = {
                        'id': version['id'],
                        'title': version['title'],
                        'state': version['state'],
                        'ghostlisted': version['ghostlisted'],
                        'version': version['version'],
                        'content_source': version['content_source'],
                        'location': location,
                        'torrent_id': version['filled_by_torrent_id'] or 'N/A',
                        'action': 'excluded',
                        'excluded_pattern': matched_pattern
                    }
                    if media_type == 'show':
                        delete_data['season_number'] = version['season_number'] if 'season_number' in version.keys() else 'N/A'
                        delete_data['episode_number'] = version['episode_number'] if 'episode_number' in version.keys() else 'N/A'
                    group_delete_items.append(delete_data)
                    results.append(delete_data)
                else:
                    # Item will be deleted - separate by state for proper cleanup
                    if version['state'] in ('Collected', 'Upgrading'):
                        items_to_delete_with_cleanup.append(version['id'])
                    else:
                        items_to_delete_database_only.append(version['id'])

                    total_deleted += 1
                    delete_data = {
                        'id': version['id'],
                        'title': version['title'],
                        'state': version['state'],
                        'ghostlisted': version['ghostlisted'],
                        'version': version['version'],
                        'content_source': version['content_source'],
                        'location': location,
                        'torrent_id': version['filled_by_torrent_id'] or 'N/A'
                    }
                    if media_type == 'show':
                        delete_data['season_number'] = version['season_number'] if 'season_number' in version.keys() else 'N/A'
                        delete_data['episode_number'] = version['episode_number'] if 'episode_number' in version.keys() else 'N/A'
                    group_delete_items.append(delete_data)
                    results.append(delete_data)  # Keep for backwards compatibility

            # Create group structure
            if group_keep_items and group_delete_items:
                group_data = {
                    'keep_items': group_keep_items,
                    'delete_items': group_delete_items
                }
                groups.append(group_data)

        # Perform actual deletions after collecting all items
        if not dry_run and (items_to_delete_with_cleanup or items_to_delete_database_only):
            # Pause queue for cleanup operations (not for dry run)
            needs_pause = (
                len(items_to_delete_with_cleanup) > 5 or  # Large batch
                len(items_to_delete_with_cleanup) > 0  # Any items with physical files
            )

            if needs_pause:
                if program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
                    if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                        program_runner.pause_info = {
                            "reason_string": "Duplicate version cleanup in progress",
                            "error_type": "SYSTEM_MAINTENANCE",
                            "service_name": "Manage Duplicates",
                            "status_code": None,
                            "retry_count": 0
                        }
                        program_runner.pause_queue()
                        paused_queue = True
                        logging.info(f"[CLEANUP_DUPLICATES] Queue paused for {len(items_to_delete_with_cleanup)} collected item deletion")

            # Delete Collected/Upgrading items with full cleanup (bypass ghostlist mode)
            if items_to_delete_with_cleanup:
                logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} Collected/Upgrading items with full cleanup (bypassing ghostlist mode)")
                debrid_provider = get_debrid_provider()
                deletion_manager = DeletionManager(debrid_provider=debrid_provider)

                # Use skip_database=True to bypass ghostlist mode
                # We'll handle database deletion ourselves below
                deletion_result = deletion_manager.delete_multiple_items(
                    item_ids=items_to_delete_with_cleanup,
                    blacklist=False,
                    blacklist_sources=False,
                    delete_from_media_server=True,
                    delete_files=True,
                    delete_from_debrid=True,
                    delete_symlinks=True,
                    clear_cache=True,
                    remove_from_content_source=False,
                    skip_database=True,  # Skip DeletionManager's database operation (bypasses ghostlist mode)
                    force_delete_parent_folder=False,
                    plex_deletion_type=None
                )

                if not deletion_result['success']:
                    logging.error(f"[CLEANUP_DUPLICATES] Deletion errors: {deletion_result.get('errors', [])}")
                    if deletion_result.get('database_locked'):
                        logging.error("[CLEANUP_DUPLICATES] Database lock detected during deletion")

                # Now delete from database (bypasses ghostlist mode)
                logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_with_cleanup)} items from database (force delete)")
                placeholders = ','.join(['?'] * len(items_to_delete_with_cleanup))
                cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_with_cleanup)

            # Delete Blacklisted/other items (database only - no files to clean up)
            if items_to_delete_database_only:
                logging.info(f"[CLEANUP_DUPLICATES] Deleting {len(items_to_delete_database_only)} Blacklisted/other items (database only)")
                placeholders = ','.join(['?'] * len(items_to_delete_database_only))
                cursor.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", items_to_delete_database_only)

            conn.commit()
        elif not dry_run:
            conn.commit()

        elapsed_time = time.time() - start_time

        version_mode_text = "same version" if version_match == 'same' else "any version"

        if keep_action == 'keep_blacklisted':
            action_description = f"collected versions ({version_mode_text})"
        else:
            action_description = f"blacklisted versions ({version_mode_text})"

        # Build message with excluded count if applicable
        message = f'{"Would delete" if dry_run else "Deleted"} {total_deleted} {action_description} from {total_movies} {media_label}'
        if total_excluded > 0:
            message += f', excluded {total_excluded} items by pattern'

        return {
            'success': True,
            'message': message,
            'total_movies': total_movies,
            'total_deleted': total_deleted,
            'total_excluded': total_excluded,
            'elapsed_time': round(elapsed_time, 2),
            'dry_run': dry_run,
            'version_match': version_match,
            'keep_action': keep_action,
            'results': results,  # Return all results for pagination (flat list)
            'groups': groups  # Return grouped data for better display
        }, 200

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        logging.error(f"Error in cleanup_failed_upgrades: {str(e)}", exc_info=True)
        return {
            'success': False,
            'error': f'An error occurred: {str(e)}'
        }, 500
    finally:
        # Resume queue if it was paused
        if paused_queue and program_runner:
            if hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
                program_runner.resume_queue()
                logging.info("[CLEANUP_DUPLICATES] Queue resumed")

        if conn:
            conn.close()
