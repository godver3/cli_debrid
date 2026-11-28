"""Build and store a file-to-episode assignment."""

from queues.media_matcher import MediaMatcher
import logging
from cli_battery.app import trakt_client
import os
import re
from scraper.functions.ptt_parser import parse_with_ptt
import json
from fuzzywuzzy import fuzz
import asyncio
from utilities.web_scraper import get_media_meta
from ._common import _assignment_store, _assignment_store_lock, _mark_items_in_library, _order_change_blocked, _saved_episode_order, _seasons_for_order
from .item_create import create_episode_item, create_full_series_items, create_movie_item, create_season_items

def _video_files(files):
    """The .mkv/.mp4/.avi files of a debrid file list, as the assignment screen uses them."""
    video_files = []
    for f in files:
        path = f.get('path', '')
        if isinstance(path, str):
            path = path.lstrip('/')
            if any(path.lower().endswith(ext) for ext in ['.mkv', '.mp4', '.avi']):
                filename = os.path.basename(path)
                video_files.append({
                    'id': f.get('id'),
                    'path': path,
                    'filename': filename,
                    'bytes': f.get('bytes', 0)
                })
        else:
             logging.warning(f"Skipping file due to non-string path: {f}")
    return video_files

def _episode_list(selected):
    """(season, episode) pairs from "S:E" strings; bad entries are dropped."""
    pairs = []
    for raw in selected or []:
        season, _, episode = str(raw).partition(':')
        if season.strip().isdigit() and episode.strip().isdigit():
            pairs.append((int(season), int(episode)))
    return pairs

class _AssignmentError(Exception):
    """A request the assignment screen can't be built for (message, HTTP status)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def _build_assignment(video_files, tmdb_id, media_type, title, year, version, torrent_id, magnet_link,
                      selection_type, selected_seasons, season, episode, requested_order):
    """Target items for the assignment screen with suggested files from the
    auto-match. Returns (target_items, episode_order); raises _AssignmentError."""
    # Get metadata
    from metadata.metadata import get_metadata
    metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
    if not metadata:
        logging.warning(f"Could not get metadata for TMDB ID {tmdb_id}. Trying with dummy IMDb ID...")
        # Generate a dummy IMDb ID and try to get basic metadata
        dummy_imdb_id = f"tt{tmdb_id.zfill(7)}"
        logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id}")

        # Try to get basic TMDB metadata using get_media_meta
        try:
            media_meta_tuple = asyncio.run(asyncio.to_thread(
                get_media_meta, str(tmdb_id), media_type
            ))

            if media_meta_tuple:
                poster_path, overview, genres, vote_average, backdrop_path = media_meta_tuple
                # Create minimal metadata with dummy IMDb ID
                metadata = {
                    'tmdb_id': tmdb_id,
                    'imdb_id': dummy_imdb_id,
                    'title': title,  # Use the title from the form
                    'year': year,    # Use the year from the form
                    'genres': genres or [],
                    'overview': overview or '',
                    'poster': poster_path,
                    'runtime': None,
                    'release_date': None
                }
                logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id}")
            else:
                # Create even more minimal metadata if get_media_meta fails
                metadata = {
                    'tmdb_id': tmdb_id,
                    'imdb_id': dummy_imdb_id,
                    'title': title,  # Use the title from the form
                    'year': year,    # Use the year from the form
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
                'title': title,  # Use the title from the form
                'year': year,    # Use the year from the form
                'genres': [],
                'overview': '',
                'poster': None,
                'runtime': None,
                'release_date': None
            }
            logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id} (fallback)")

    # Fetch TV show season data if needed
    alt_seasons_data = None
    episode_order = None
    if media_type in ['tv', 'show']:
        # Which season numbers this request actually needs covered, so the
        # fallback can trigger not just when Trakt returns nothing at all, but
        # also when it returns data that's simply missing the requested season
        # (e.g. a recently-aired season Trakt hasn't indexed yet).
        if selection_type == 'seasons':
            needed_seasons = {int(s) for s in selected_seasons if s.strip().isdigit()}
        elif selection_type == 'episode_list':
            needed_seasons = {s for s, _ in _episode_list(selected_seasons)}
        elif selection_type != 'all' and season and season.strip().isdigit():
            needed_seasons = {int(season)}
        else:
            needed_seasons = set()

        def _covers_needed_seasons(data):
            if not data:
                return False
            if not needed_seasons:
                return True
            return needed_seasons.issubset({int(k) for k in data.keys()})

        seasons_data, source = None, None
        show_imdb_id = metadata.get('imdb_id')
        blocked = _order_change_blocked(show_imdb_id, requested_order)
        if blocked:
            raise _AssignmentError(blocked, 409)
        episode_order = requested_order or _saved_episode_order(show_imdb_id)
        if episode_order != 'default':
            # Number the items the way the chosen order does; the default
            # order is kept to read packs that use aired numbering.
            seasons_data = _seasons_for_order(show_imdb_id, episode_order)
            if not seasons_data:
                raise _AssignmentError(f'Could not fetch the episode list for episode order {episode_order}.', 503)
            alt_seasons_data = _seasons_for_order(show_imdb_id, 'default')
            source = episode_order
        else:
            try:
                seasons_data, source = trakt_client.get_show_seasons_and_episodes(show_imdb_id, include_specials=True)
                logging.debug(f"Fetched seasons data (incl specials) from {source} for {show_imdb_id} in prepare_manual_assignment")
            except Exception as e:
                # Log but don't necessarily fail, maybe proceed without detailed episode titles
                logging.error(f"Error fetching season data from Trakt (non-critical): {str(e)}")

        if episode_order == 'default' and not _covers_needed_seasons(seasons_data):
            # Trakt unset/unauthorized/rate-limited, or simply missing the
            # requested season: fall back to TVDB (or Trakt again via the same
            # client) through the shared metadata provider used elsewhere in the
            # app, so manual-assign for TV doesn't hard-depend on a direct,
            # unfallbacked Trakt call or Trakt's current season coverage.
            try:
                from cli_battery.app.direct_api import DirectAPI
                fallback_seasons_data, source = DirectAPI.get_show_seasons(metadata.get('imdb_id'))
                logging.info(f"Trakt missing needed season data; used fallback provider '{source}' for {metadata.get('imdb_id')} in prepare_manual_assignment")
                if fallback_seasons_data:
                    # Merge rather than replace, so seasons Trakt did have (with
                    # richer episode titles) aren't discarded in favor of a
                    # thinner fallback response.
                    merged = dict(seasons_data or {})
                    merged.update(fallback_seasons_data)
                    seasons_data = merged
            except Exception as fallback_e:
                logging.error(f"Fallback season fetch (TVDB/TMDB) also failed for {metadata.get('imdb_id')}: {fallback_e}")

        if seasons_data:
            metadata['seasons'] = {}
            # Convert integer keys to string keys for internal use
            for season_num, season_info in seasons_data.items():
                metadata['seasons'][season_num] = {
                    'episodes': season_info.get('episodes', {}),
                    'episode_count': len(season_info.get('episodes', {}))
                }
        else:
            logging.warning(f"No season data fetched from any provider for {metadata.get('imdb_id')}")

    # --- ADD LOGGING HERE ---
    logging.debug(f"Metadata seasons before creating items: {json.dumps(metadata.get('seasons', {}), indent=2)}")
    # --- END LOGGING ---

    # Determine target media items based on selection
    target_items = []
    if media_type == 'movie':
        item = create_movie_item(metadata, title, year, version, torrent_id, magnet_link)
        item['item_key'] = f"movie_{item['tmdb_id']}"
        target_items.append(item)
    else: # TV Show
        if selection_type == 'all':
            target_items = create_full_series_items(metadata, title, year, version, torrent_id, magnet_link)
        elif selection_type == 'seasons':
            target_items = create_season_items(metadata, title, year, version, torrent_id, magnet_link, selected_seasons)
        elif selection_type == 'episode_list':
            # The episodes another torrent left without a file ("fill the gaps")
            target_items = [create_episode_item(metadata, title, year, version, torrent_id, magnet_link, s, e)
                            for s, e in _episode_list(selected_seasons)]
        else: # Single episode
            try:
                season_number = int(season)
                episode_number = int(episode)
                target_items = [create_episode_item(metadata, title, year, version, torrent_id, magnet_link, season_number, episode_number)]
            except (ValueError, TypeError):
                # Return JSON error
                raise _AssignmentError('Invalid season or episode number provided.', 400)

        # Add unique keys
        for item in target_items:
             item['item_key'] = f"ep_{item['tmdb_id']}_s{item['season_number']:02d}e{item['episode_number']:02d}"

    if not target_items:
        error_msg = 'Could not determine target media items based on selection.'
        logging.error(f"Failed to determine target items. Selection: {selection_type}, Seasons: {selected_seasons}, S/E: {season}/{episode}")
        # Return JSON error
        raise _AssignmentError(error_msg, 400)

    # --- SORT target_items --- 
    def sort_key(item):
        if item.get('type') == 'movie':
            # Assign a low sort order for movies to appear first
            return (-1, -1)
        else:
            # Sort by season then episode number
            return (item.get('season_number', 999), item.get('episode_number', 999))

    target_items.sort(key=sort_key)
    _mark_items_in_library(target_items, metadata.get('imdb_id'), version)
    logging.debug(f"Sorted target_items: {[item.get('item_key', 'N/A') for item in target_items]}")
    # --- END SORT ---

    # --- Re-add PTT Parsing ---
    logging.info("Parsing video file names using PTT...")
    parsed_video_files = []
    for f in video_files:
        # Only parse if path is a non-empty string
        file_path = f.get('path')
        if isinstance(file_path, str) and file_path:
            filename = os.path.basename(file_path)
            logging.debug(f"Parsing filename: '{filename}' (from path: '{file_path}')")
            parsed = parse_with_ptt(filename)
            if not parsed.get('parsing_error'):
                parsed_video_files.append({
                    'original': f, # Store the original file dict
                    'parsed': parsed,
                })
            else:
                logging.warning(f"PTT parsing error for filename '{filename}': {parsed.get('parsing_error')}")
        else:
            logging.warning(f"Skipping file parsing due to invalid path: {f.get('path')}")
    logging.info(f"Successfully parsed {len(parsed_video_files)} video file names.")
    # --- End Re-add PTT Parsing ---

    # --- Start Auto-assignment Logic ---
    logging.info("Attempting automatic file assignment...")

    # Add flags to track usage/assignment
    for f_info in parsed_video_files:
        f_info['used'] = False
    for item in target_items:
        item['assigned'] = False
        item['suggested_file_path'] = None # Ensure initialization

    assignment_count = 0

    # Absolute numbers files state next to their own season ("S13E01 (0422)")
    # outrank the S/E: a file naming another episode's absolute isn't this one
    from utilities.manual_assign_episodes import (absolute_numbers, explicit_absolute_conflicts,
                                                  file_explicit_absolute, is_anime_op_ed_extra,
                                                  phase2_title_too_weak)
    # NCED/NCOP extras parse as episode numbers and steal S/E + absolute matches
    # from the real files (Judas Gintama: eps 1-30 left unassigned).
    for f_info in parsed_video_files:
        name = f_info['original'].get('filename') or ''
        if is_anime_op_ed_extra(name) or is_anime_op_ed_extra(f_info['original'].get('path') or ''):
            f_info['used'] = True
            f_info['original']['skip_reason'] = 'OP/ED extra'
            logging.debug(f"Skipping anime OP/ED extra for auto-assign: {f_info['original'].get('path') or name}")
    show_absolute_numbers = {}
    if media_type in ['tv', 'show']:
        for f_info in parsed_video_files:
            f_info['explicit_absolute'] = file_explicit_absolute(
                f_info['parsed'], f_info['original'].get('filename'), metadata.get('seasons'))
        if any(f_info['explicit_absolute'] for f_info in parsed_video_files):
            show_absolute_numbers = absolute_numbers(metadata.get('seasons'))
    # Show in another episode order: "S15E01" files are aired S15E01, which may
    # be another episode than the show's S15E01 (Phase 1b places them by ID)
    aired_order_map = None
    if alt_seasons_data:
        from scraper.functions.episode_order_mapping import OrderMapping
        aired_order_map = OrderMapping(metadata.get('seasons'), alt_seasons_data)

    # --- Phase 1: S/E Matching and Movie Matching ---
    logging.info("Phase 1: Attempting S/E and Movie matching...")
    _media_matcher = MediaMatcher()
    for item in target_items:
        if item['assigned']: # Skip if already assigned (e.g., by movie logic below?)
            continue

        item_type = item.get('type')

        # --- Movie Logic: Assign title-matched (else largest) file ---
        if item_type == 'movie':
            if video_files: # Ensure there are video files to check
                # Find the largest *unused* video file
                unused_parsed_files = [f for f in parsed_video_files if not f['used']]
                if unused_parsed_files:
                    # A multi-movie pack (e.g. a franchise box-set with several
                    # films as separate files) bundles unrelated movies together -
                    # picking the largest unused file unconditionally assigns
                    # whichever film happens to be biggest to every movie item,
                    # regardless of which one was actually requested. Prefer
                    # files whose parsed title+year match this item; only fall
                    # back to plain largest-file-wins when nothing matches (e.g.
                    # a single-file torrent whose release name doesn't
                    # fuzzy-match cleanly), preserving prior behavior there.
                    title_matched_files = [
                        f for f in unused_parsed_files
                        if _media_matcher.match_movie(f['parsed'], item, f['original'].get('filename', ''))
                    ]
                    match_candidates = title_matched_files or unused_parsed_files
                    if not title_matched_files:
                        logging.warning(
                            f"[Phase 1] No file title-matched movie '{item.get('title')}' ({item.get('year')}) "
                            f"among {len(unused_parsed_files)} unused file(s) - falling back to largest unused file"
                        )
                    # Find original file dict corresponding to largest matching (or unused) parsed file
                    largest_parsed_file_info = max(match_candidates, key=lambda f: f['original'].get('bytes', 0))
                    largest_filename = largest_parsed_file_info['original'].get('filename')
                    if largest_filename:
                         item['suggested_file_path'] = largest_filename
                         item['assigned'] = True
                         item['match_reason'] = 'movie' if title_matched_files else 'largest'
                         largest_parsed_file_info['used'] = True # Mark this file as used
                         assignment_count += 1
                         logging.info(f"[Phase 1] Auto-assigned largest unused file '{largest_parsed_file_info['original'].get('path')}' to movie '{item.get('title')}'")
                    else:
                         logging.warning(f"[Phase 1] Could not assign largest file for movie '{item.get('title')}' - filename missing.")
                else:
                     logging.warning(f"[Phase 1] Cannot assign largest file for movie '{item.get('title')}' - no unused video files found.")
            else:
                logging.warning(f"[Phase 1] Cannot assign largest file for movie '{item.get('title')}' - no video files found at all.")
            continue # Move to the next item

        # --- Episode S/E Logic ---
        elif item_type == 'episode':
            potential_matches = []
            item_season = item.get('season_number')
            item_episode = item.get('episode_number')

            if item_season is None or item_episode is None:
                logging.warning(f"[Phase 1] Skipping S/E match for item {item.get('item_key')} due to missing S/E numbers.")
                continue

            # Iterate through UNUSED parsed files
            item_absolute_numbers = show_absolute_numbers.get((item_season, item_episode))
            for file_info in [f for f in parsed_video_files if not f['used']]:
                if explicit_absolute_conflicts(item_absolute_numbers, file_info.get('explicit_absolute')):
                    continue
                if aired_order_map is not None and not file_info.get('explicit_absolute'):
                    aired = aired_order_map.aired_positions(file_info['parsed'].get('seasons') or [],
                                                            file_info['parsed'].get('episodes') or [])
                    if aired and aired_order_map.aired_of(item_season, item_episode) not in aired:
                        continue
                parsed = file_info['parsed']
                match = False
                parsed_seasons = parsed.get('seasons', [])
                parsed_episodes = parsed.get('episodes', [])

                try:
                    season_match_strict = (item_season in parsed_seasons)
                    episode_match = (item_episode in parsed_episodes)
                    season_match = season_match_strict
                    if not parsed_seasons and item_season == 1:
                        season_match = True
                    if season_match and episode_match:
                        match = True
                except Exception as match_err:
                     logging.error(f"[Phase 1] Error during S/E matching logic for item {item.get('item_key')} and file {file_info['original']['path']}: {match_err}", exc_info=True)

                if match:
                    potential_matches.append(file_info)

            # Assign if exactly one unique S/E match is found among unused files
            if len(potential_matches) == 1:
                match_info = potential_matches[0]
                suggested_filename = match_info['original'].get('filename')
                if suggested_filename:
                    item['suggested_file_path'] = suggested_filename
                    item['assigned'] = True
                    item['match_reason'] = 'se'
                    match_info['used'] = True # Mark file as used
                    assignment_count += 1
                    logging.info(f"[Phase 1] Auto-assigned file '{match_info['original']['path']}' to item '{item.get('item_key')}' based on S/E match.")
                else:
                     logging.warning(f"[Phase 1] Could not assign S/E matched file '{match_info['original']['path']}' to item '{item.get('item_key')}' - filename missing.")
            elif len(potential_matches) > 1:
                 logging.warning(f"[Phase 1] Found {len(potential_matches)} potential S/E file matches among unused files for item '{item.get('item_key')}'. Leaving unassigned for Phase 2. Files: {[p['original']['path'] for p in potential_matches]}")

        # --- Handle other item types ---
        else:
             logging.warning(f"[Phase 1] Skipping assignment for unrecognized item type: {item_type} for item key {item.get('item_key')}")

    logging.info(f"Phase 1 Complete. Assigned {assignment_count} items based on S/E or Movie logic.")

    # --- Phase 1b: absolute episode numbers (anime packs) ---
    if media_type in ['tv', 'show']:
        from utilities.manual_assign_episodes import match_absolute_files
        absolute_count = match_absolute_files(target_items, parsed_video_files,
                                              metadata.get('seasons'), alt_seasons_data)
        assignment_count += absolute_count
        logging.info(f"Phase 1b Complete. Assigned {absolute_count} items by absolute episode number.")

    # --- Phase 2: Title Matching Fallback (File-Centric) ---
    logging.info("Phase 2: Attempting Title matching for remaining items...")
    phase2_assignment_count = 0
    MATCH_THRESHOLD = 85 # Same threshold as before, adjust if needed

    # Get unassigned items (episodes only, as movies handled in Phase 1)
    # Make a copy to allow removal while iterating
    unassigned_items = [item for item in target_items if not item['assigned'] and item['type'] == 'episode']
    # Get unused files (using the parsed_video_files structure which links to original)
    unused_files_info = [f_info for f_info in parsed_video_files if not f_info['used']]

    logging.debug(f"[Phase 2] Starting with {len(unassigned_items)} unassigned episode items and {len(unused_files_info)} unused files.")

    # Iterate through each UNUSED file
    for file_info in unused_files_info:
        original_file = file_info['original']
        raw_filename = original_file.get('filename', '')
        if not raw_filename:
            logging.debug(f"[Phase 2] Skipping file {original_file.get('path')} as it has no filename.")
            continue

        # Clean the filename for matching
        cleaned_filename_base, _ = os.path.splitext(raw_filename)
        cleaned_filename_base = cleaned_filename_base.lower()

        # --- AGGRESSIVE CLEANING --- 
        # Remove content in brackets/parentheses
        cleaned_filename = re.sub(r'[\(\[].*?[\)\]]', '', cleaned_filename_base)
        # Remove common resolutions/quality tags (simplified)
        cleaned_filename = re.sub(r'\b(480p|720p|1080p|2160p|4k|uhd|[0-9]{3,4}x[0-9]{3,4})\b', '', cleaned_filename, flags=re.IGNORECASE)
        # Remove common codecs/formats (simplified)
        cleaned_filename = re.sub(r'\b(x264|h264|x265|h265|aac|dts|ac3|web-dl|webrip|bluray|remux)\b', '', cleaned_filename, flags=re.IGNORECASE)

        # --- NEW: Remove S/E patterns --- 
        # e.g., s01e01, s1e1, season 1 episode 1, etc.
        cleaned_filename = re.sub(r'\b(s(\d{1,2})e(\d{1,3})|season\s*\d{1,2}\s*episode\s*\d{1,3})\b', '', cleaned_filename, flags=re.IGNORECASE)
        # --- END NEW ---

        # Remove trailing hyphens/dots/spaces often left after cleaning/before release group
        cleaned_filename = re.sub(r'[\s._-]+$', '', cleaned_filename).strip()
        # --- END AGGRESSIVE CLEANING ---

        logging.debug(f"[Phase 2] Processing file: '{raw_filename}' (BaseClean: '{cleaned_filename_base}', AggressiveClean: '{cleaned_filename}')")

        # After stripping S/E tags, a leftover that is only the show title
        # ("gintama") token_set_ratio-matches every episode title that
        # contains the show name at 100 and mis-assigns leftovers.
        if phase2_title_too_weak(cleaned_filename, metadata.get('title')):
            logging.debug(f"[Phase 2] Skipping '{raw_filename}' - cleaned name is only the series title.")
            continue

        best_match_item = None
        best_match_score = 0
        best_match_item_index = -1 # Index within the current unassigned_items list

        # Compare this file against all currently UNASSIGNED episode items
        for idx, item in enumerate(unassigned_items):
            item_episode_title = (item.get('episode_title') or '').lower()
            item_key = item.get('item_key', 'N/A')

            # Skip generic titles or items without titles
            if not item_episode_title or item_episode_title == f'episode {item.get("episode_number")}':
                continue

            # Calculate fuzzy match score
            # --- CHANGE: Use token_set_ratio --- 
            score = fuzz.token_set_ratio(item_episode_title, cleaned_filename)
            # --- END CHANGE ---

            # Check if this is the best match *for this file* so far and meets threshold
            if score > best_match_score and score >= MATCH_THRESHOLD:
                best_match_score = score
                best_match_item = item
                best_match_item_index = idx

        # After checking all unassigned items for the current file:
        # If we found a best match above the threshold for this file
        if best_match_item is not None:
            # Assign this file to that best matching item
            best_match_item['suggested_file_path'] = original_file['filename']
            best_match_item['assigned'] = True
            best_match_item['match_reason'] = 'title'
            best_match_item['match_score'] = best_match_score
            file_info['used'] = True # Mark the file_info dict (containing original+parsed) as used
            phase2_assignment_count += 1
            assignment_count += 1 # Increment total count

            logging.info(f"[Phase 2] Auto-assigned file '{original_file.get('path')}' to item '{best_match_item.get('item_key')}' based on FUZZY FILENAME match ('{best_match_item.get('episode_title')}' vs AGGRESSIVELY CLEANED '{cleaned_filename}', Score: {best_match_score}).")

            # Remove the assigned item from the list so it can't be matched by another file
            del unassigned_items[best_match_item_index]
            logging.debug(f"[Phase 2] Removed assigned item {best_match_item.get('item_key')} from pool. Remaining unassigned: {len(unassigned_items)}")

    logging.info(f"Phase 2 Complete. Assigned {phase2_assignment_count} additional items based on Title logic.")
    # --- End Phase 2 ---

    logging.info(f"Completed automatic assignment attempt. Suggested assignments for {assignment_count} out of {len(target_items)} items.")

    # What each file's name says it holds, for the page's file picker and summary
    from utilities.manual_assign_episodes import file_position_label
    for f_info in parsed_video_files:
        parsed = f_info['parsed']
        f_info['original'].update({
            'label': file_position_label(parsed, f_info.get('explicit_absolute')),
            'seasons': list(parsed.get('seasons') or []),
            'episodes': list(parsed.get('episodes') or []),
            'absolutes': list(f_info.get('explicit_absolute') or []),
        })
    return target_items, episode_order

def _store_assignment(target_items, episode_order, requested_order, imdb_id, video_files, context, notice=''):
    """Keep a built assignment for show_manual_assignment; returns its token."""
    import uuid as _uuid
    token = str(_uuid.uuid4())
    with _assignment_store_lock:
        _assignment_store[token] = {
            'target_items': target_items,
            'video_files': video_files,
            'magnet_link': context['magnet_link'],
            'torrent_filename': context['torrent_filename'],
            'torrent_id': context['torrent_id'],
            'version': context['version'],
            'is_torrent_file': context['is_torrent_file'],
            # Saved on the show when the assignment is confirmed
            'episode_order': requested_order if requested_order and episode_order
                             and requested_order != _saved_episode_order(imdb_id) else '',
            # The order the items are numbered in, and what rematch needs to rebuild them
            'current_episode_order': episode_order or '',
            'rematch_context': context,
            # Shown at the top: what the previous screen's confirm said (fill the gaps)
            'notice': notice,
        }
    return token
