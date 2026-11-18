"""Deletion impact preview, bulk delete and show delete."""

from flask import jsonify, request
from ..models import user_required, admin_required
from utilities.settings import get_setting
from debrid import get_debrid_provider
import logging
from ._common import library_bp

# =============================================================================
# SHARED DELETION API ENDPOINTS
# Used by: Library multi-select, Maintenance page, Show multi-level deletion
# =============================================================================

@library_bp.route('/check_deletion_impact', methods=['POST'])
@user_required
def check_deletion_impact():
    """
    Check the impact of deleting items before performing deletion
    Used by all deletion features to preview what will be deleted

    Request JSON:
    {
        "item_ids": [1, 2, 3],           # Database IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"],
        "blacklist": false,               # Whether to blacklist items
        "blacklist_sources": false        # Whether to blacklist content sources
    }

    Returns impact analysis with counts and details for each layer
    """
    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_items_by_ids

        data = request.json
        item_ids = data.get('item_ids', [])
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)
        blacklist_sources = data.get('blacklist_sources', False)

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No items specified for deletion'
            }), 400

        # Get items from database
        items = get_items_by_ids(item_ids)

        if not items:
            return jsonify({
                'success': False,
                'error': 'No items found with the specified IDs'
            }), 404

        # Initialize deletion manager
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)

        # Build impact analysis
        impact = {
            'success': True,
            'items_count': len(items),
            'items': [],
            'layers': {}
        }

        # Analyze each item
        for item in items:
            item_impact = {
                'id': item['id'],
                'title': item.get('title', 'Unknown'),
                'type': item.get('type', 'unknown'),
                'state': item.get('state', 'Unknown'),
                'layers': {}
            }

            # Check each layer
            if 'database' in layers:
                item_impact['layers']['database'] = {
                    'will_delete': True,
                    'description': 'Database entry'
                }

            if 'media_server' in layers:
                # Check if item exists in media server
                media_server_check = deletion_manager.remove_from_media_server(item)
                item_impact['layers']['media_server'] = {
                    'will_delete': media_server_check.get('found', False),
                    'description': f"Media server ({deletion_manager.media_server})",
                    'details': media_server_check
                }

            if 'filesystem' in layers:
                # Check filesystem impact
                file_path = item.get('filled_by_file') or item.get('location_on_disk')
                item_impact['layers']['filesystem'] = {
                    'will_delete': bool(file_path),
                    'description': 'File on disk',
                    'file_path': file_path,
                    'file_size': None  # Could add size calculation here
                }

            if 'debrid' in layers:
                torrent_id = item.get('filled_by_torrent_id', '') or ''
                is_nzb = str(torrent_id).startswith('nzb:')
                debrid_hash = item.get('filled_by_magnet_hash') if not is_nzb else torrent_id
                item_impact['layers']['debrid'] = {
                    'will_delete': bool(is_nzb or debrid_hash),
                    'description': 'Usenet NZB (cli_mount)' if is_nzb else 'Debrid torrent',
                    'hash': debrid_hash,
                    'is_nzb': is_nzb,
                }

            if 'symlinks' in layers and deletion_manager.using_symlinks:
                # Check symlink impact
                symlink_check = deletion_manager._check_symlinks(item)
                item_impact['layers']['symlinks'] = {
                    'will_delete': symlink_check.get('found', False),
                    'description': 'Symbolic links',
                    'details': symlink_check
                }

            if 'cache' in layers:
                # Check content source cache
                source_check = deletion_manager.check_content_sources(item)
                item_impact['layers']['cache'] = {
                    'will_delete': len(source_check.get('cache_files', [])) > 0,
                    'description': 'Content source cache',
                    'sources': source_check.get('sources', []),
                    'cache_files': source_check.get('cache_files', [])
                }

            impact['items'].append(item_impact)

        # Aggregate layer statistics
        for layer in layers:
            layer_stats = {
                'affected_count': 0,
                'items': []
            }

            for item_impact in impact['items']:
                if layer in item_impact['layers'] and item_impact['layers'][layer].get('will_delete', False):
                    layer_stats['affected_count'] += 1
                    layer_stats['items'].append({
                        'id': item_impact['id'],
                        'title': item_impact['title']
                    })

            # Add layer-specific counts
            if layer == 'filesystem':
                layer_stats['files_count'] = layer_stats['affected_count']
            elif layer == 'debrid':
                layer_stats['torrents_count'] = layer_stats['affected_count']
                layer_stats['nzb_count'] = sum(1 for ii in impact['items'] if ii['layers'].get('debrid', {}).get('is_nzb'))
                layer_stats['torrent_only_count'] = layer_stats['torrents_count'] - layer_stats['nzb_count']
            elif layer == 'symlinks':
                layer_stats['symlinks_count'] = layer_stats['affected_count']

            impact['layers'][layer] = layer_stats

        # Add blacklist recommendations
        if blacklist or blacklist_sources:
            impact['blacklist_info'] = {
                'will_blacklist_items': blacklist,
                'will_blacklist_sources': blacklist_sources,
                'affected_sources': []
            }

            if blacklist_sources:
                # Collect unique sources
                sources_set = set()
                for item_impact in impact['items']:
                    if 'cache' in item_impact['layers']:
                        for source in item_impact['layers']['cache'].get('sources', []):
                            sources_set.add(source)
                impact['blacklist_info']['affected_sources'] = list(sources_set)

        logging.info(f"Deletion impact check completed for {len(items)} items")
        return jsonify(impact)

    except Exception as e:
        logging.error(f"Error checking deletion impact: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@library_bp.route('/delete_items', methods=['POST'])
@admin_required
def delete_items():
    """
    Delete multiple items with specified layers and options
    Used by all deletion features (library multi-select, maintenance, show multi-level)

    Request JSON:
    {
        "item_ids": [1, 2, 3],           # Database IDs to delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache"],
        "blacklist": false,               # Whether to blacklist items
        "blacklist_sources": false,       # Whether to blacklist content sources
        "remove_from_content_source": false  # Whether to remove from Trakt/Overseerr/etc.
    }

    Returns deletion results with success/failure for each item and layer
    """
    from routes.program_operation_routes import get_program_runner

    # Track if we paused the queue
    paused_queue = False
    program_runner = None

    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_items_by_ids
        from utilities.settings import get_setting

        data = request.json
        item_ids = data.get('item_ids', [])
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)
        blacklist_sources = data.get('blacklist_sources', False)
        # Use global setting for content source removal (ignores frontend request value)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)

        if not item_ids:
            return jsonify({
                'success': False,
                'error': 'No items specified for deletion'
            }), 400

        # Get items from database
        items = get_items_by_ids(item_ids)

        if not items:
            return jsonify({
                'success': False,
                'error': 'No items found with the specified IDs'
            }), 404

        # Check auto-ghostlist setting (applies to all content types)
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_ITEMS] Auto-ghostlist setting: {auto_ghostlist}")
        logging.info(f"[DELETE_ITEMS] Processing {len(item_ids)} items")

        # Determine if we need to pause queue
        # Pause for large batches or operations with physical files
        needs_pause = (
            len(item_ids) > 5 or  # Large batch
            'filesystem' in layers or  # Physical file operations
            'debrid' in layers or
            'media_server' in layers
        )

        # Pause queue if needed (prevents race conditions during long operations)
        program_runner = get_program_runner()
        if needs_pause and program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
            if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                logging.info(f"[DELETE_ITEMS] Pausing queue for batch deletion of {len(item_ids)} items (layers: {layers})")
                program_runner.pause_info = {
                    "reason_string": "Library batch deletion in progress",
                    "error_type": "SYSTEM_MAINTENANCE",
                    "service_name": "Library Delete",
                    "status_code": None,
                    "retry_count": 0
                }
                program_runner.pause_queue()
                paused_queue = True
                logging.info("[DELETE_ITEMS] Queue paused successfully")
            else:
                logging.info("[DELETE_ITEMS] Queue pause requested but pause_queue method not available")
        else:
            if not needs_pause:
                logging.info(f"[DELETE_ITEMS] Queue pause not needed for {len(item_ids)} items with layers {layers}")
            elif not program_runner:
                logging.info("[DELETE_ITEMS] Program runner not available for queue pause")
            elif not hasattr(program_runner, 'is_running') or not program_runner.is_running():
                logging.info("[DELETE_ITEMS] Program runner not running, queue pause skipped")

        # Initialize deletion manager
        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Skip database layer - we'll handle it ourselves based on auto_ghostlist
        result = deletion_manager.delete_multiple_items(
            item_ids=item_ids,
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server='media_server' in layers,
            delete_files='filesystem' in layers,
            delete_from_debrid='debrid' in layers,
            delete_symlinks='symlinks' in layers,
            clear_cache='cache' in layers,
            remove_from_content_source=remove_from_content_source,
            skip_database=True  # We'll handle database ourselves based on auto_ghostlist
        )

        logging.info(f"[DELETE_ITEMS] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_ITEMS] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_ITEMS] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_ITEMS] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            return jsonify({
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_ITEMS] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if 'database' in layers:
            if auto_ghostlist:
                # Ghostlist: Update items to ghostlisted=1 and state=Blacklisted
                logging.info(f"[DELETE_ITEMS] Taking GHOSTLIST path")
                try:
                    from datetime import datetime
                    from database.core import get_db_connection

                    conn = get_db_connection()
                    cursor = conn.cursor()

                    cursor.execute('BEGIN TRANSACTION')

                    # Ghostlist all specified items
                    placeholders = ','.join('?' * len(item_ids))
                    cursor.execute(
                        f'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Blacklisted', datetime.now()] + item_ids
                    )

                    updated_count = cursor.rowcount
                    conn.commit()
                    conn.close()

                    auto_ghostlisted = True
                    logging.info(f"[DELETE_ITEMS] Ghostlisted {updated_count} item(s)")

                    # Also add each unique imdb_id to manual blacklist
                    try:
                        from database.manual_blacklist import add_to_manual_blacklist
                        seen_imdb = {}
                        for item in items:
                            iid = item.get('imdb_id')
                            if iid and iid not in seen_imdb:
                                seen_imdb[iid] = item
                        for iid, item in seen_imdb.items():
                            media_type = 'episode' if item.get('type') == 'episode' else 'movie'
                            add_to_manual_blacklist(
                                imdb_id=iid,
                                media_type=media_type,
                                title=item.get('title', ''),
                                year=str(item.get('year', ''))
                            )
                            logging.info(f"[DELETE_ITEMS] Added {iid} to manual blacklist")
                    except Exception as mb_err:
                        logging.warning(f"[DELETE_ITEMS] Could not add to manual blacklist: {mb_err}")

                except Exception as e:
                    logging.error(f"[DELETE_ITEMS] Failed to ghostlist: {e}")
                    if conn:
                        conn.rollback()
                        conn.close()

            else:
                # Delete: Remove items from database
                logging.info(f"[DELETE_ITEMS] Taking DELETE path")
                try:
                    from database.database_writing import remove_from_media_items

                    deleted_count = 0
                    for item_id in item_ids:
                        if remove_from_media_items(item_id):
                            deleted_count += 1

                    logging.info(f"[DELETE_ITEMS] Deleted {deleted_count} item(s) from database")
                except Exception as e:
                    logging.error(f"[DELETE_ITEMS] Failed to delete from database: {e}")

        # Check for database lock errors
        if result.get('database_locked'):
            logging.error("[DELETE_ITEMS] Database lock detected during batch deletion")
            return jsonify({
                'success': False,
                'error': 'database is locked',
                'database_locked': True
            }), 503

        # Add auto_ghostlisted flag to result
        result['auto_ghostlisted'] = auto_ghostlisted

        if result['success']:
            logging.info(f"[DELETE_ITEMS] Successfully processed {result['deleted_count']} items")
        else:
            logging.error(f"[DELETE_ITEMS] Deletion completed with errors: {result.get('errors', [])}")

        return jsonify(result)

    except Exception as e:
        logging.error(f"[DELETE_ITEMS] Error deleting items: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    finally:
        # ALWAYS resume queue if we paused it
        if paused_queue and program_runner:
            if hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
                logging.info("[DELETE_ITEMS] Resuming queue in finally block")
                program_runner.resume_queue()
                logging.info("[DELETE_ITEMS] Queue resumed successfully")
            else:
                logging.warning("[DELETE_ITEMS] Queue was paused but resume_queue method not available")

@library_bp.route('/delete_show/<imdb_id>', methods=['POST'])
@admin_required
def delete_show(imdb_id):
    """
    Delete entire show (all seasons and episodes)

    Request body:
    {
        "blacklist": bool,  # Whether to blacklist instead of permanent delete
        "layers": ["database", "media_server", "filesystem", "debrid", "symlinks", "cache", "content_source"]
    }
    """
    from routes.program_operation_routes import get_program_runner
    import sqlite3

    # Track if we paused the queue
    paused_queue = False
    program_runner = None

    try:
        from utilities.deletion_manager import DeletionManager
        from database.database_reading import get_all_episodes_for_show

        data = request.get_json() or {}
        layers = data.get('layers', ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache'])
        blacklist = data.get('blacklist', False)

        # Get all episodes for this show
        episodes = get_all_episodes_for_show(imdb_id)

        if not episodes:
            return jsonify({
                'success': False,
                'error': 'No episodes found for this show'
            }), 404

        episode_ids = [ep['id'] for ep in episodes]

        # Convert layers array to individual flags
        delete_from_media_server = 'media_server' in layers
        delete_files = 'filesystem' in layers
        delete_from_debrid = 'debrid' in layers
        delete_symlinks = 'symlinks' in layers
        clear_cache = 'cache' in layers
        # Use global setting for content source removal (ignores frontend layers)
        remove_from_content_source = get_setting('Library Manager', 'remove_from_content_sources', True)
        blacklist_sources = data.get('blacklist_sources', False)

        logging.info(f"[DELETE_SHOW] Starting deletion for show {imdb_id}")
        logging.info(f"[DELETE_SHOW] Found {len(episode_ids)} episodes to delete")
        logging.info(f"[DELETE_SHOW] Layers requested: {layers}")

        # ONLY check auto-ghostlist setting - this determines database behavior
        auto_ghostlist = get_setting('Library Manager', 'ghostlist_mode', False)
        auto_ghostlisted = False

        logging.info(f"[DELETE_SHOW] Auto-ghostlist setting: {auto_ghostlist} (type: {type(auto_ghostlist)})")

        # Additional debug - check what the setting value actually is
        if auto_ghostlist:
            logging.info(f"[DELETE_SHOW] WILL GHOSTLIST - auto_ghostlist is truthy")
        else:
            logging.info(f"[DELETE_SHOW] WILL DELETE - auto_ghostlist is falsy")

        # Determine if we need to pause queue
        # Pause for show deletions with physical operations
        needs_pause = (
            len(episode_ids) > 5 or  # Large show
            delete_files or  # Physical file operations
            delete_from_debrid or
            delete_from_media_server
        )

        # Pause queue if needed (prevents race conditions during long operations)
        program_runner = get_program_runner()
        if needs_pause and program_runner and hasattr(program_runner, 'is_running') and program_runner.is_running():
            if hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue):
                logging.info(f"[DELETE_SHOW] Pausing queue for show deletion of {len(episode_ids)} episodes")
                program_runner.pause_info = {
                    "reason_string": f"Deleting show {imdb_id}",
                    "error_type": "SYSTEM_MAINTENANCE",
                    "service_name": "Show Delete",
                    "status_code": None,
                    "retry_count": 0
                }
                program_runner.pause_queue()
                paused_queue = True
                logging.info("[DELETE_SHOW] Queue paused successfully")
            else:
                logging.info("[DELETE_SHOW] Queue pause requested but pause_queue method not available")
        else:
            if not needs_pause:
                logging.info(f"[DELETE_SHOW] Queue pause not needed for {len(episode_ids)} episodes")
            elif not program_runner:
                logging.info("[DELETE_SHOW] Program runner not available for queue pause")
            elif not hasattr(program_runner, 'is_running') or not program_runner.is_running():
                logging.info("[DELETE_SHOW] Program runner not running, queue pause skipped")

        # Handle content source removal
        content_source_result = None
        if remove_from_content_source and episodes:
            logging.info(f"[DELETE_SHOW] Removing show from content sources")
            debrid_provider = get_debrid_provider()
            deletion_manager = DeletionManager(debrid_provider=debrid_provider)
            show_item = episodes[0].copy()
            show_item['type'] = 'show'
            content_source_result = deletion_manager.remove_from_content_source(show_item)
            logging.info(f"[DELETE_SHOW] Content source removal result: {content_source_result.get('message')}")

        # Handle physical cleanup layers (Plex, files, debrid, symlinks, cache)
        # Get show title from first episode for Plex content-level deletion
        show_title = episodes[0].get('title') if episodes else None
        # tmdb_id falls back to imdb_id if not available
        show_tmdb_id = (episodes[0].get('tmdb_id') or imdb_id) if episodes else imdb_id

        debrid_provider = get_debrid_provider()
        deletion_manager = DeletionManager(debrid_provider=debrid_provider)
        result = deletion_manager.delete_multiple_items(
            item_ids=episode_ids,
            blacklist=blacklist,
            blacklist_sources=blacklist_sources,
            delete_from_media_server=delete_from_media_server,
            delete_files=delete_files,
            delete_from_debrid=delete_from_debrid,
            delete_symlinks=delete_symlinks,
            clear_cache=clear_cache,
            remove_from_content_source=False,  # Already handled above
            force_delete_parent_folder=True,  # Whole show delete - remove show folder
            skip_database=True,  # We'll handle database ourselves based on auto_ghostlist
            # Plex content-level deletion params - deletes whole show in 1 API call
            plex_deletion_type='show',
            plex_content_title=show_title,
            plex_imdb_id=imdb_id,
            plex_tmdb_id=show_tmdb_id
        )

        logging.info(f"[DELETE_SHOW] Physical cleanup completed: {result.get('deleted_count', 0)} items processed")

        # CRITICAL CHECK: Verify physical cleanup succeeded before proceeding to database
        success = result.get('success')

        if success is None:
            # BUG: success key is missing from result - this should never happen
            logging.error(f"[DELETE_SHOW] BUG: 'success' key missing from delete_multiple_items result. Result: {result}")
            return jsonify({
                'success': False,
                'error': 'Internal error: deletion result malformed',
                'errors': ['Missing success key in deletion result']
            }), 500

        if not success:
            # Expected: CRITICAL CHECK aborted physical cleanup (Plex deletion failed)
            # Do NOT proceed to database or content source removal to prevent orphaned entries
            logging.error(f"[DELETE_SHOW] Physical cleanup failed - aborting all remaining operations to prevent orphaned Plex entries")
            logging.error(f"[DELETE_SHOW] Errors: {result.get('errors', [])}")

            errors_list = result.get('errors', ['Physical cleanup failed'])
            response_data = {
                'success': False,
                'error': errors_list[0] if errors_list else 'Physical cleanup failed',
                'errors': errors_list,
                'deleted_count': 0,
                'failed_count': result.get('failed_count', 0)
            }
            if result.get('plex_not_found'):
                response_data['plex_not_found'] = True
            return jsonify(response_data), 500

        # NOW handle database (LAST step) - ghostlist OR delete based on setting
        logging.info(f"[DELETE_SHOW] DATABASE LAYER - auto_ghostlist={auto_ghostlist}")

        if auto_ghostlist:
            # Ghostlist: Update all episodes to ghostlisted=1 and state=Blacklisted
            logging.info(f"[DELETE_SHOW] Taking GHOSTLIST path (batch UPDATE)")
            try:
                from database.core import get_db_connection
                from datetime import datetime

                conn = get_db_connection()
                cursor = conn.cursor()

                # BEGIN TRANSACTION
                cursor.execute('BEGIN TRANSACTION')

                # BATCH UPDATE - Ghostlist all episodes in one statement
                cursor.execute(
                    'UPDATE media_items SET ghostlisted = 1, state = ?, last_updated = ? WHERE imdb_id = ? AND type = ?',
                    ['Blacklisted', datetime.now(), imdb_id, 'episode']
                )

                updated_count = cursor.rowcount

                # COMMIT TRANSACTION
                conn.commit()
                conn.close()

                auto_ghostlisted = True
                logging.info(f"[DELETE_SHOW] Ghostlisted {updated_count} episodes for show {imdb_id} - ghostlisted=1, state=Blacklisted")

                # Also add to manual blacklist to block future episodes
                try:
                    from database.manual_blacklist import add_to_manual_blacklist
                    first_ep = episodes[0]
                    add_to_manual_blacklist(
                        imdb_id=imdb_id,
                        media_type='episode',
                        title=first_ep.get('title', ''),
                        year=str(first_ep.get('year', ''))
                    )
                    logging.info(f"[DELETE_SHOW] Added {imdb_id} to manual blacklist")
                except Exception as mb_err:
                    logging.warning(f"[DELETE_SHOW] Could not add to manual blacklist: {mb_err}")

            except sqlite3.OperationalError as e:
                # Rollback and check for database lock
                if conn:
                    conn.rollback()
                    conn.close()

                if "database is locked" in str(e):
                    logging.error(f"[DELETE_SHOW] Database locked during ghostlist of show {imdb_id}")
                    return jsonify({
                        'success': False,
                        'error': 'database is locked',
                        'database_locked': True
                    }), 503
                else:
                    logging.error(f"[DELETE_SHOW] OperationalError during ghostlist: {e}")
                    return jsonify({
                        'success': False,
                        'error': f'Database error during ghostlist: {str(e)}'
                    }), 500

            except Exception as e:
                logging.error(f"[DELETE_SHOW] Failed to ghostlist: {e}")
                if conn:
                    conn.rollback()
                    conn.close()
                return jsonify({
                    'success': False,
                    'error': f'Failed to ghostlist show: {str(e)}'
                }), 500

        else:
            # Delete: Remove all episodes from database using BATCH DELETE
            logging.info(f"[DELETE_SHOW] Taking DELETE path (batch DELETE)")
            try:
                from database.database_writing import delete_items_batch

                # BATCH DELETE - All episodes in one transaction
                db_result = delete_items_batch(
                    item_ids=episode_ids,
                    blacklist=False
                )

                if db_result['success']:
                    deleted_count = db_result['deleted_count']
                    logging.info(f"[DELETE_SHOW] Deleted {deleted_count} episodes from database")
                else:
                    # Database deletion failed
                    if db_result.get('database_locked'):
                        logging.error(f"[DELETE_SHOW] Database locked during deletion of show {imdb_id}")
                        return jsonify({
                            'success': False,
                            'error': 'database is locked',
                            'database_locked': True
                        }), 503
                    else:
                        logging.error(f"[DELETE_SHOW] Database deletion failed: {db_result['error']}")
                        return jsonify({
                            'success': False,
                            'error': f"Failed to delete from database: {db_result['error']}"
                        }), 500

            except Exception as e:
                logging.error(f"[DELETE_SHOW] Failed to delete from database: {e}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to delete show: {str(e)}'
                }), 500

        # Build detailed layer execution summary (only report actual successes)
        layers_executed = []
        layers_skipped = []  # Track skipped/failed layers

        # Database layer - ghostlist or delete
        if auto_ghostlisted:
            layers_executed.append('Database (Ghostlisted)')
        else:
            layers_executed.append('Database (Deleted)')

        # Calculate file count first (needed for Media Server logic)
        file_count = sum(len(r.get('deleted_files', [])) for r in result.get('results', []))

        # Media Server - check for content-level deletion (plex_deleted flag) or per-item removal
        media_server_removed = result.get('plex_deleted', False) or any(
            r.get('media_server_removed') and r.get('media_server_type', 'none') != 'none'
            for r in result.get('results', [])
        )
        if delete_from_media_server:
            if media_server_removed:
                layers_executed.append('Media Server')
            elif file_count > 0:
                # Files were deleted, so if Plex removal failed with "not found",
                # it's because we just removed it - treat as successful removal
                layers_executed.append('Media Server')
            else:
                # No files deleted and Plex removal failed - item was never in Plex (blacklisted, etc.)
                # Check if Plex is configured
                plex_url = get_setting('Plex', 'url', '')
                if not plex_url:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'Plex not configured'})
                else:
                    layers_skipped.append({'layer': 'Media Server', 'reason': 'No items found on server'})

        # Check file collection management mode for appropriate deletion messages
        file_management = get_setting('File Management', 'file_collection_management', default='Plex')

        # Filesystem - only if files actually deleted
        if delete_files:
            if file_count > 0:
                layers_executed.append('Filesystem')
            else:
                # In Plex mode, filesystem layer is not applicable since Plex manages files
                if file_management == 'Plex':
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'Not applicable in Plex mode'})
                else:
                    layers_skipped.append({'layer': 'Filesystem', 'reason': 'No files found'})

        # Debrid - check for batch removal (debrid_removed flag) or per-item removal
        debrid_removed = result.get('debrid_removed', False) or any(r.get('debrid_removed', False) for r in result.get('results', []))
        if delete_from_debrid:
            if debrid_removed:
                torrent_count = result.get('debrid_torrents_removed', 0)
                nzb_count = result.get('debrid_nzb_removed', 0) or sum(r.get('debrid_nzb_removed', 0) for r in result.get('results', []))
                if nzb_count == 0 and torrent_count == 0:
                    # single-item path: infer from per-item results
                    for r in result.get('results', []):
                        if r.get('debrid_removed'):
                            if r.get('debrid_nzb_removed', 0): nzb_count += 1
                            else: torrent_count += 1
                _parts = []
                if torrent_count > 0: _parts.append(f'{torrent_count} torrent{"s" if torrent_count != 1 else ""}')
                if nzb_count > 0: _parts.append(f'{nzb_count} NZB{"s" if nzb_count != 1 else ""}')
                layers_executed.append(f'Debrid/Usenet ({", ".join(_parts)})' if _parts else 'Debrid/Usenet')
            else:
                # Check if debrid provider is available
                debrid_provider = get_debrid_provider()
                if not debrid_provider:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'Provider not configured'})
                else:
                    layers_skipped.append({'layer': 'Debrid/Usenet', 'reason': 'No torrents/NZBs found'})

        # Symlinks - check if deleted or cleaned up (only show in Symlink mode)
        symlink_count = sum(len(r.get('deleted_symlinks', [])) for r in result.get('results', []))
        if delete_symlinks and file_management != 'Plex':
            # If symlinks were found and deleted, or if operation succeeded (cleanup happened)
            if symlink_count > 0 or result['success']:
                layers_executed.append('Symlinks')
            else:
                layers_skipped.append({'layer': 'Symlinks', 'reason': 'No symlinks found'})

        # Cache - always succeeds if requested
        if clear_cache:
            layers_executed.append('Cache')

        # Content Source - report based on actual result
        if remove_from_content_source and content_source_result:
            if content_source_result.get('success'):
                layers_executed.append(f"Content Source ({', '.join(content_source_result.get('sources_succeeded', []))})")
            else:
                layers_executed.append('Content Source (Failed)')

        # Build response with both 'error' (singular) and 'errors' (plural) for frontend compatibility
        errors_list = result.get('errors', [])
        response_data = {
            'success': result['success'],
            'deleted_count': result.get('deleted_count', 0),
            'failed_count': result.get('failed_count', 0),
            'errors': errors_list,
            'layers_executed': layers_executed,
            'layers_skipped': layers_skipped,  # New field for skipped layers
            'content_source_removal': content_source_result,
            'auto_ghostlisted': auto_ghostlisted,  # Flag if show was auto-ghostlisted
            'message': f"{'Ghostlisted' if auto_ghostlisted else 'Deleted'} {result.get('deleted_count', 0)} episodes from show"
        }

        # Add 'error' (singular) field for frontend compatibility - uses first error if available
        if errors_list:
            response_data['error'] = errors_list[0]

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"[DELETE_SHOW] Error deleting show {imdb_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    finally:
        # ALWAYS resume queue if we paused it
        if paused_queue and program_runner:
            if hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
                logging.info("[DELETE_SHOW] Resuming queue in finally block")
                program_runner.resume_queue()
                logging.info("[DELETE_SHOW] Queue resumed successfully")
            else:
                logging.warning("[DELETE_SHOW] Queue was paused but resume_queue method not available")
