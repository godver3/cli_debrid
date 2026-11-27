"""Confirm a manual assignment and add leftovers to Wanted."""

from flask import request, jsonify
from debrid import get_debrid_provider
from database.database_writing import add_media_item, replace_collected_item
from ..models import admin_required
from datetime import datetime
import logging
from cli_battery.app import trakt_client
import re
from database.torrent_tracking import update_torrent_tracking
from content_checkers.content_source_detail import append_content_source_detail
import json
from ._common import _order_change_blocked, _requested_episode_order, _save_episode_order, _saved_episode_order, _seasons_for_order, _sync_plex_order_for_new_show, magnet_bp
from .item_create import create_episode_item, create_movie_item

#: Room for about 10,000 episodes (a few fields each beyond the per-episode ones).
CONFIRM_MAX_FORM_PARTS = 20_000
CONFIRM_MAX_FORM_MEMORY_SIZE = 16 * 1024 * 1024

@magnet_bp.route('/confirm_manual_assignment', methods=['POST'])
@admin_required
def confirm_manual_assignment():
    """Confirm the manual file assignments and add items to the database."""
    # One form field per episode: a long show's pack (Pokémon, 1,240 episodes)
    # passes Flask's defaults of 1,000 parts / 500 KB and fails with a 413.
    # Raise them for this view only, before the form is parsed.
    request.max_form_parts = CONFIRM_MAX_FORM_PARTS
    request.max_form_memory_size = CONFIRM_MAX_FORM_MEMORY_SIZE
    try:
        # Get data submitted from the manual assignment form
        assignments = request.form.to_dict(flat=False) # Get as dict of lists
        
        # Extract common data
        magnet_link = assignments.pop('magnet_link', [None])[0]
        torrent_filename = assignments.pop('torrent_filename', [None])[0]
        initial_torrent_id = assignments.pop('torrent_id', [None])[0]
        version = assignments.pop('version', [None])[0]
        is_torrent_file = assignments.pop('is_torrent_file', [False])[0]
        requested_order = _requested_episode_order(assignments.pop('episode_order', [''])[0])
        # "Replace episodes already in the library": a Collected row in this version
        # is pointed at the new file instead of getting a second copy next to it
        replace_existing = assignments.pop('replace_existing', [''])[0] in ('1', 'on', 'true')
        # Items left with no file (not ignored) are added to Wanted, so they're searched for
        empty_to_wanted = assignments.pop('empty_to_wanted', [''])[0] in ('1', 'on', 'true')
        empty_keys = [k for k, v in assignments.items() if not (v and v[0])]

        if not all([torrent_filename, initial_torrent_id, version]):
             return jsonify({'success': False, 'error': 'Missing essential torrent information in submission.'}), 400

        # An episode order picked on the assign page: save it on the show before
        # any episode is added, so the items, the battery's seasons and later
        # scrapes all use it. Only allowed while the show has no episodes yet.
        order_imdb_id = None
        order_plex_note = None
        if requested_order:
            order_tmdb_id = next((k.split('_')[1] for k in assignments if k.startswith('ep_') and len(k.split('_')) >= 3), None)
            if order_tmdb_id:
                from metadata.metadata import get_imdb_id_if_missing
                order_imdb_id = get_imdb_id_if_missing({'tmdb_id': int(order_tmdb_id), 'media_type': 'show'})
            if order_imdb_id:
                blocked = _order_change_blocked(order_imdb_id, requested_order)
                if blocked:
                    return jsonify({'success': False, 'error': blocked}), 409
                if requested_order != _saved_episode_order(order_imdb_id):
                    _save_episode_order(order_imdb_id, order_tmdb_id, requested_order)
                else:
                    order_imdb_id = None

        # Extract torrent hash
        torrent_hash = None
        if magnet_link and magnet_link.startswith('magnet:'):
            hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
            if hash_match:
                torrent_hash = hash_match.group(1).lower()
        elif is_torrent_file:
            # For torrent files, we need to extract hash from the torrent ID
            # This is a limitation - we don't have the original torrent file to extract hash
            # We'll use the torrent ID as a fallback for tracking purposes
            logging.info(f"Using torrent ID {initial_torrent_id} as hash for torrent file tracking")
            torrent_hash = f"torrent_file_{initial_torrent_id}"

        # De-dupe: if this exact submission (same hash, same target item(s)) was
        # already confirmed moments ago, don't re-add the magnet or recreate items
        # again — a retried/duplicate POST (double-click, page retry after the
        # earlier missing-torrent bug, etc.) would otherwise add another permanent
        # torrent and duplicate DB rows every time. Scoped to the same tmdb_id, not
        # just the same hash: a multi-movie collection magnet (e.g. a franchise
        # box-set) is legitimately re-assigned multiple times for different target
        # movies in quick succession, each a separate Assign flow sharing one
        # magnet — that must not trip this guard. Only guards against near-immediate
        # re-submission of the exact same item; a legitimate re-assignment of the
        # same hash+item later on (e.g. after deletion) is unaffected.
        if torrent_hash and not is_torrent_file:
            from database.torrent_tracking import get_torrent_history
            current_tmdb_ids = set()
            for item_key in assignments.keys():
                parts = item_key.split('_')
                if len(parts) >= 2:
                    current_tmdb_ids.add(parts[1])
            try:
                history = get_torrent_history(torrent_hash)
            except Exception:
                history = []
            for record in history:
                if record['trigger_source'] != 'manual_assign_confirm':
                    continue
                try:
                    prior_item_data = json.loads(record['item_data']) if record['item_data'] else {}
                except (ValueError, TypeError):
                    prior_item_data = {}
                prior_tmdb_id = str(prior_item_data.get('tmdb_id') or '')
                if prior_tmdb_id and current_tmdb_ids and prior_tmdb_id not in current_tmdb_ids:
                    # Same magnet, different target item (e.g. a different movie from
                    # the same collection pack) - not a duplicate submission.
                    continue
                try:
                    record_time = datetime.fromisoformat(str(record['timestamp']))
                    if (datetime.now() - record_time).total_seconds() < 120:
                        # Only block if that prior assignment's items are still actually
                        # present - if the user deleted them (e.g. to redo a bad
                        # assignment) there's nothing left to duplicate, so a fresh
                        # confirm within the window must be allowed to go through
                        # regardless of how recently the deleted attempt was.
                        prior_torrent_id = prior_item_data.get('torrent_id')
                        still_present = True
                        if prior_torrent_id and prior_tmdb_id:
                            try:
                                from database import get_db_connection as _gdb_dedupe
                                _conn_dedupe = _gdb_dedupe()
                                try:
                                    still_present = bool(_conn_dedupe.execute(
                                        "SELECT 1 FROM media_items WHERE filled_by_torrent_id = ? AND tmdb_id = ? LIMIT 1",
                                        (prior_torrent_id, prior_tmdb_id)
                                    ).fetchone())
                                finally:
                                    _conn_dedupe.close()
                            except Exception as _presence_err:
                                logging.warning(f"Dedup presence check failed for torrent_id={prior_torrent_id!r}, assuming still present: {_presence_err}")
                                still_present = True
                        if not still_present:
                            logging.info(f"Prior manual assignment for hash {torrent_hash} tmdb_id={prior_tmdb_id!r} was deleted - allowing re-assignment despite recent timestamp.")
                            continue
                        logging.warning(f"Duplicate manual assignment submission detected for hash {torrent_hash} tmdb_id={prior_tmdb_id!r} ({(datetime.now() - record_time).total_seconds():.0f}s after prior confirm); ignoring re-submission.")
                        return jsonify({'success': False, 'error': 'This torrent was already assigned moments ago. Refresh the page before assigning again.'}), 409
                except (ValueError, TypeError):
                    continue

        # initial_torrent_id came from get_torrent_file_list's disposable preview add,
        # which is deleted from the debrid service immediately after listing files
        # (see get_torrent_file_list's finally block). Re-add the magnet for real BEFORE
        # creating any DB items, so items are never created pointing at an already-deleted
        # torrent ID (which the checking queue would then report as missing/404 and reset
        # to Wanted).
        final_torrent_id = initial_torrent_id
        debrid_provider = None
        # Nothing picked (all ignored, or only "add the rest to Wanted"): no item will
        # point at the torrent, so don't add it for real and leave an orphan behind
        any_file_picked = any(v and v[0] and v[0] != '--ignore--' for v in assignments.values())
        if magnet_link and any_file_picked:
            try:
                logging.info(f"Adding permanent magnet torrent to debrid service for manual assignment: {magnet_link[:60]}...")
                debrid_provider = get_debrid_provider()
                readd_result_id = debrid_provider.add_torrent(magnet_link)
                if readd_result_id:
                    final_torrent_id = readd_result_id
                    logging.info(f"Successfully added permanent magnet torrent with ID: {final_torrent_id}")
                else:
                    logging.error(f"Failed to add permanent magnet torrent for manual assignment: {magnet_link[:60]}...")
                    return jsonify({'success': False, 'error': 'Failed to add torrent to debrid service. It may already be added, or the debrid provider rejected it.'}), 503
            except Exception as readd_error:
                logging.error(f"Error adding permanent magnet torrent for manual assignment: {readd_error}", exc_info=True)
                return jsonify({'success': False, 'error': f'Failed to add torrent to debrid service: {readd_error}'}), 503
        # else: torrent-file uploads have no magnet to re-add; the file was already
        # added for real (not a disposable preview) in prepare_manual_assignment,
        # so initial_torrent_id is already permanent for that path.

        # Resolve pack title if provider named a multi-file pack after one of its files
        # (e.g. TorBox naming a pack "Pokemon.S01E75...mkv").
        selected_files = [
            v[0] for v in assignments.values()
            if v and v[0] and v[0] != '--ignore--'
        ]
        if len(selected_files) > 1:
            from debrid.common.utils import _looks_like_pack_file_name, resolve_torrent_title
            if _looks_like_pack_file_name(torrent_filename):
                resolved = resolve_torrent_title({
                    'title': torrent_filename,
                    'files': [{'path': p} for p in selected_files],
                })
                if resolved == torrent_filename and final_torrent_id:
                    try:
                        _provider = debrid_provider or get_debrid_provider()
                        _tinfo = _provider.get_torrent_info(final_torrent_id) if _provider else None
                        if _tinfo and _tinfo.get('files'):
                            resolved = resolve_torrent_title({
                                'title': torrent_filename,
                                'files': _tinfo['files'],
                            })
                    except Exception as _title_err:
                        logging.debug(f"Could not inspect torrent files for pack title resolution: {_title_err}")
                torrent_filename = resolved

        added_items_count = 0
        replaced_items_count = 0
        failed_items_count = 0
        processed_items_info = [] # To store info for notifications
        _ordered_seasons_cache = {}  # imdb_id -> seasons in the show's episode order
        successfully_added_items = [] # Store IDs of added items
        representative_tracking_item_data = None # Store data for tracking update

        # Each key in assignments (excluding common data) should be an item_key
        # The value will be a list containing the selected file path
        for item_key, selected_file_list in assignments.items():
            selected_filename = selected_file_list[0] if selected_file_list else None
            
            # Skip if no file was selected for this item
            if not selected_filename or selected_filename == '--ignore--':
                logging.info(f"Skipping item {item_key} as no file was selected or set to ignore.")
                continue

            # Reconstruct the item data based on item_key (this is complex)
            # We need to re-fetch metadata and rebuild the item based on the key parts
            try:
                parts = item_key.split('_')
                item_type = parts[0]
                tmdb_id = parts[1]
                
                # Re-fetch metadata (can be optimized by caching or passing more data)
                media_type_lookup = 'movie' if item_type == 'movie' else 'show'
                from metadata.metadata import get_metadata
                metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type_lookup)
                if not metadata:
                    logging.warning(f"Could not re-fetch metadata for {item_key}. Trying with dummy IMDb ID...")
                    # Generate a dummy IMDb ID and create minimal metadata
                    dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
                    logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")
                    
                    # Try to get TMDB metadata first, then create metadata with dummy IMDb ID
                    try:
                        from metadata.metadata import get_tmdb_metadata
                        media_meta_tuple = get_tmdb_metadata(str(tmdb_id), media_type_lookup)

                        logging.info(f"Media meta tuple: {media_meta_tuple}")
                        title = media_meta_tuple.get('title')
                        year = media_meta_tuple.get('year')
                        genres = media_meta_tuple.get('genres')
                        
                        if media_meta_tuple:
                            # Create metadata with TMDB data and dummy IMDb ID
                            metadata = {
                                'tmdb_id': tmdb_id,
                                'imdb_id': 'tt0000000',
                                'title': title, 
                                'year': year,
                                'genres': genres or [],
                            }
                            logging.info(f"Created metadata with TMDB data and dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id}")
                        else:
                            # Fallback to minimal metadata if get_media_meta fails
                            metadata = {
                                'tmdb_id': tmdb_id,
                                'imdb_id': dummy_imdb_id,
                                'title': f'TMDB {tmdb_id}',  # Fallback title
                                'year': None,
                                'genres': [],
                                'overview': '',
                                'poster': None,
                                'runtime': None,
                                'release_date': None
                            }
                            logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (no TMDB meta available)")
                    except Exception as meta_error:
                        logging.error(f"Error getting media meta for TMDB ID {tmdb_id}: {meta_error}")
                        # Create minimal metadata as fallback
                        metadata = {
                            'tmdb_id': tmdb_id,
                            'imdb_id': dummy_imdb_id,
                            'title': f'TMDB {tmdb_id}',  # Fallback title
                            'year': None,
                            'genres': [],
                            'overview': '',
                            'poster': None,
                            'runtime': None,
                            'release_date': None
                        }
                        logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (fallback)")
                
                # Base data
                title = metadata.get('title')
                year = metadata.get('year')

                if item_type == 'movie':
                    # Use magnet_link if available, otherwise use None for torrent files
                    item_magnet_link = magnet_link if magnet_link else None
                    item_data = create_movie_item(metadata, title, year, version, final_torrent_id, item_magnet_link)
                elif item_type == 'ep':
                    # Expecting format like 's01e13' in parts[2]
                    if len(parts) < 3:
                        logging.error(f"Invalid item key format for episode: {item_key}. Expected at least 3 parts.")
                        failed_items_count += 1
                        continue
                    
                    se_part = parts[2] # e.g., s01e13
                    match = re.search(r's(\d+)e(\d+)', se_part, re.IGNORECASE)
                    if not match:
                         logging.error(f"Could not parse season/episode from item key part: '{se_part}' in key '{item_key}'")
                         failed_items_count += 1
                         continue

                    try:
                        season_number = int(match.group(1))
                        episode_number = int(match.group(2))
                    except (ValueError, IndexError):
                         logging.error(f"Error converting parsed season/episode to int from '{se_part}' in key '{item_key}'", exc_info=True)
                         failed_items_count += 1
                         continue

                    # A non-default episode order: titles and air dates come
                    # from that order's episode list, never Trakt's numbering.
                    show_order = _saved_episode_order(metadata.get('imdb_id'))
                    if show_order != 'default':
                        ordered = _ordered_seasons_cache.get(metadata.get('imdb_id'))
                        if ordered is None:
                            ordered = _seasons_for_order(metadata.get('imdb_id'), show_order) or {}
                            _ordered_seasons_cache[metadata.get('imdb_id')] = ordered
                        if ordered:
                            metadata['seasons'] = {sn: {'episodes': si.get('episodes', {}), 'episode_count': len(si.get('episodes', {}))}
                                                   for sn, si in ordered.items()}

                    # Re-fetch season data if necessary — also re-fetch (rather than
                    # trust an existing but incomplete 'seasons' dict) when the
                    # specific season this item needs isn't covered yet.
                    if 'seasons' not in metadata or season_number not in metadata.get('seasons', {}):
                        seasons_data, source = None, None
                        try:
                            seasons_data, source = trakt_client.get_show_seasons_and_episodes(metadata.get('imdb_id'), include_specials=True)
                            logging.debug(f"Fetched seasons data (incl specials) from {source} for {metadata.get('imdb_id')} in confirm_manual_assignment")
                        except Exception as e:
                             logging.warning(f"Could not re-fetch season data from Trakt for {item_key}: {e}")
                             # Proceed cautiously without detailed episode info

                        if not seasons_data or season_number not in seasons_data:
                            try:
                                from cli_battery.app.direct_api import DirectAPI
                                fallback_seasons_data, source = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                                logging.info(f"Trakt missing needed season data; used fallback provider '{source}' for {metadata.get('imdb_id')} in confirm_manual_assignment")
                                if fallback_seasons_data:
                                    merged = dict(seasons_data or {})
                                    merged.update(fallback_seasons_data)
                                    seasons_data = merged
                            except Exception as fallback_e:
                                logging.warning(f"Fallback season fetch also failed for {item_key}: {fallback_e}")

                        if seasons_data:
                            metadata['seasons'] = {}
                            # Convert integer keys to string keys
                            for sn, si in seasons_data.items():
                                metadata['seasons'][sn] = {'episodes': si.get('episodes', {}), 'episode_count': len(si.get('episodes', {}))}
                        else:
                            logging.warning(f"No season data fetched from any provider for {metadata.get('imdb_id')} in confirmation step")

                    # Use magnet_link if available, otherwise use None for torrent files
                    item_magnet_link = magnet_link if magnet_link else None
                    item_data = create_episode_item(metadata, title, year, version, final_torrent_id, item_magnet_link, season_number, episode_number)
                else:
                    logging.warning(f"Unrecognized item key format: {item_key}")
                    failed_items_count += 1
                    continue
                    
                # Assign the manually selected FILENAME
                item_data['filled_by_file'] = selected_filename # Store only the filename
                item_data['filled_by_title'] = torrent_filename # Use the overall torrent filename
                
                # Add to database (remove internal/matcher keys first)
                db_item = {k: v for k, v in item_data.items() if k not in [
                    'series_title', 'season', 'episode', 'series_year', 'media_type', '_matcher_data', 'item_key'
                ]}
                
                item_id = replace_collected_item(db_item) if replace_existing else None
                if item_id:
                    replaced_items_count += 1
                else:
                    item_id = add_media_item(db_item, user_initiated=True)
                if item_id:
                    added_items_count += 1
                    successfully_added_items.append(item_id)
                    # Prepare data for notification
                    processed_items_info.append({
                        'id': item_id,
                        'title': db_item.get('title', 'Unknown Title'),
                        'type': db_item.get('type', 'unknown'),
                        'year': db_item.get('year', ''),
                        'version': db_item.get('version', ''),
                        'season_number': db_item.get('season_number'),
                        'episode_number': db_item.get('episode_number'),
                        'new_state': 'Checking', # Assume it goes to Checking
                        'is_upgrade': False,
                        'upgrading_from': None,
                        'content_source': db_item.get('content_source'),
                        'content_source_detail': db_item.get('content_source_detail')
                    })
                    logging.info(f"Successfully added item {item_key} with file {selected_filename}, torrent ID: {final_torrent_id}")

                    # Prepare data for torrent tracking (only need one representative item)
                    if representative_tracking_item_data is None:
                        representative_tracking_item_data = {
                            'title': db_item.get('title'), 'year': db_item.get('year'),
                            'media_type': db_item.get('type'), 'version': db_item.get('version'),
                            'tmdb_id': db_item.get('tmdb_id'), 'imdb_id': db_item.get('imdb_id'),
                            'filled_by_title': torrent_filename, 'filled_by_file': selected_filename, # Use filename here too
                            'torrent_id': final_torrent_id
                        }
                        if db_item.get('type') == 'episode':
                            representative_tracking_item_data.update({'season_number': db_item.get('season_number'), 'episode_number': db_item.get('episode_number')})
                    
                else:
                    logging.error(f"Failed to add item {item_key} to database.")
                    failed_items_count += 1
            
            except Exception as item_error:
                logging.error(f"Error processing assignment for {item_key}: {item_error}", exc_info=True)
                failed_items_count += 1

        # final_torrent_id was already established (and the magnet already added for
        # real) before item creation above, so items already carry the correct ID.
        # Only torrent tracking remains to be recorded here.
        if added_items_count > 0:
            # Update torrent tracking with the final ID
            if torrent_hash and representative_tracking_item_data:
                # Update the torrent_id in the tracking data
                representative_tracking_item_data['torrent_id'] = final_torrent_id
                try:
                    update_torrent_tracking(
                        torrent_hash=torrent_hash,
                        item_data=representative_tracking_item_data,
                        trigger_details={'source': 'manual_assignment_confirm', 'user_initiated': True},
                        trigger_source='manual_assign_confirm',
                        rationale=f'User confirmed manual file assignment (Final Torrent ID: {final_torrent_id})'
                    )
                    logging.info(f"Updated torrent tracking for hash {torrent_hash} with final torrent ID {final_torrent_id}")
                except Exception as track_error:
                    logging.error(f"Failed to update torrent tracking for hash {torrent_hash}: {track_error}", exc_info=True)
            elif not torrent_hash:
                logging.warning("Could not extract torrent hash, skipping torrent tracking update.")
            elif not representative_tracking_item_data:
                 logging.warning("No representative item data found, skipping torrent tracking update.")

        # Send notifications for successfully added items
        if processed_items_info:
            try:
                from routes.notifications import send_notifications
                from routes.settings_routes import get_enabled_notifications_for_category
                from routes.extensions import app
                with app.app_context():
                    response = get_enabled_notifications_for_category('checking') # Or maybe a 'manual_add' category?
                    if response.json.get('success'):
                        enabled_notifications = response.json.get('enabled_notifications')
                        if enabled_notifications:
                            send_notifications(processed_items_info, enabled_notifications, notification_category='state_change')
            except Exception as notify_error:
                logging.error(f"Failed to send notifications after manual assignment: {notify_error}")

        wanted_count = _add_empty_to_wanted(empty_keys, version) if empty_to_wanted and empty_keys else 0

        if order_imdb_id and added_items_count > 0:
            order_plex_note = _sync_plex_order_for_new_show(order_imdb_id, requested_order)

        if added_items_count > 0:
            message = f'Successfully assigned {added_items_count} item(s).' 
            if replaced_items_count:
                message += (f' {replaced_items_count} of them replaced what was already in the library; '
                            f'the old files are removed once the new ones are collected.')
            if failed_items_count > 0:
                 message += f' Failed to assign {failed_items_count} item(s).' 
            if order_imdb_id:
                message += ' Episode order saved on the show.'
                if order_plex_note:
                    message += f' {order_plex_note}'
            if wanted_count:
                message += f' {wanted_count} item(s) with no file added to Wanted.'
            return jsonify({'success': True, 'message': message, 'added_count': added_items_count,
                            'failed_count': failed_items_count, 'wanted_count': wanted_count})
        elif wanted_count:
            message = f'{wanted_count} item(s) with no file added to Wanted.'
            if failed_items_count > 0:
                message += f' Failed to assign {failed_items_count} item(s).'
            return jsonify({'success': True, 'message': message,
                            'added_count': 0, 'failed_count': failed_items_count, 'wanted_count': wanted_count})
        elif failed_items_count > 0:
            return jsonify({'success': False, 'error': f'Failed to assign {failed_items_count} item(s). Check logs.', 'added_count': 0, 'failed_count': failed_items_count}), 500
        else:
             return jsonify({'success': False, 'error': 'No items were assigned. Did you select files?', 'added_count': 0, 'failed_count': 0}), 400

    except Exception as e:
        logging.error(f"Error confirming manual assignment: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An unexpected error occurred: {str(e)}'}), 500

def _add_empty_to_wanted(item_keys, version):
    """Add the items a manual assignment left with no file to Wanted.

    Goes through process_metadata + add_wanted_items like a Discover request,
    so release dates, unaired episodes, the blacklist and existing rows are
    handled the same way; only the episodes in `item_keys` are kept.
    Returns how many were added.
    """
    wanted = {}  # tmdb_id -> {'type': ..., 'episodes': set of (s, e)}
    for key in item_keys:
        parts = key.split('_')
        if len(parts) < 2 or parts[0] not in ('ep', 'movie'):
            continue
        entry = wanted.setdefault(parts[1], {'type': parts[0], 'episodes': set()})
        if parts[0] == 'ep' and len(parts) >= 3:
            match = re.search(r's(\d+)e(\d+)', parts[2], re.IGNORECASE)
            if match:
                entry['episodes'].add((int(match.group(1)), int(match.group(2))))
    if not wanted:
        return 0

    from metadata.metadata import get_metadata, get_imdb_id_if_missing, process_metadata
    from database.wanted_items import add_wanted_items
    versions = {(version or '').rstrip('*'): True}
    added = 0
    for tmdb_id, entry in wanted.items():
        is_movie = entry['type'] == 'movie'
        try:
            imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': 'movie' if is_movie else 'show'})
            if not imdb_id:
                logging.warning(f"Manual assign: no IMDb ID for TMDB {tmdb_id}; can't add its items to Wanted")
                continue
            metadata = get_metadata(tmdb_id=tmdb_id, item_media_type='movie' if is_movie else 'show') or {}
            request = {'imdb_id': imdb_id, 'tmdb_id': tmdb_id, 'media_type': 'movie' if is_movie else 'tv',
                       'title': metadata.get('title'), 'year': metadata.get('year'),
                       'genres': metadata.get('genres') or [], 'versions': versions}
            if not is_movie:
                request['requested_seasons'] = sorted({s for s, _ in entry['episodes']})
            processed = process_metadata([request]) or {}
            if is_movie:
                items = processed.get('movies', [])
            else:
                items = [i for i in processed.get('episodes', [])
                         if (i.get('season_number'), i.get('episode_number')) in entry['episodes']]
            for item in items:
                item['content_source'] = 'Magnet_Assigner'
                append_content_source_detail(item, source_type='Magnet_Assigner')
            count = add_wanted_items(items, versions) if items else 0
            added += count or 0
            logging.info(f"Manual assign: added {count} of {len(entry['episodes']) or 1} item(s) with no file "
                         f"for {imdb_id} to Wanted")
        except Exception as e:
            logging.error(f"Manual assign: adding items with no file for TMDB {tmdb_id} to Wanted failed: {e}",
                          exc_info=True)
    return added
