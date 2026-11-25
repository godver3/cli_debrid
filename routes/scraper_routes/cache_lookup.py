"""Cache-status checks and lookup by ID."""

from flask import jsonify, request, session
import logging
from debrid import get_debrid_provider
from ..models import user_required, onboarding_required, scraper_permission_required, scraper_view_access_required
from utilities.settings import get_setting
from database.database_reading import get_media_item_presence_overall
from queues.torrent_processor import TorrentProcessor
from typing import Dict, Any
import hashlib
import asyncio
import re
from utilities.web_scraper import get_media_meta
from typing import List
from ._common import _download_and_get_hash, _phalanx_cache_manager, scraper_bp

@scraper_bp.route('/check_cache_status', methods=['POST'])
@user_required
@scraper_permission_required
def check_cache_status():
    try:
        data = request.json
        logging.debug(f"Cache check request data: {data}")
        
        # Use the singleton cache manager
        cache_manager = _phalanx_cache_manager
        
        # Handle single item cache check (new approach)
        if 'index' in data:
            index = data.get('index')
            magnet_link = data.get('magnet_link')
            torrent_url = data.get('torrent_url')
            
            # Get imdb_id from request and extract title
            imdb_id = data.get('imdb_id')
            if not imdb_id:
                imdb_id = session.get('last_selected_imdb_id')
                if imdb_id:
                    logging.debug(f"Retrieved imdb_id {imdb_id} from session for cache check.")
            
            # Create the item dict for context if imdb_id is available
            item_for_check = {'imdb_id': imdb_id} if imdb_id else None

            logging.debug(f"Processing cache check for item at index {index}")
            
            if not magnet_link and not torrent_url:
                logging.warning(f"No magnet link or torrent URL provided for index {index}")
                return jsonify({'status': 'check_unavailable'}), 200
            
            # Extract hash from magnet link if present - do this up front
            torrent_hash = None
            file_hash = None
            
            if magnet_link:
                # Fast hash extraction
                btih_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link, re.IGNORECASE)
                if btih_match:
                    torrent_hash = btih_match.group(1).lower()
                    
                # Handle HTTP URLs - extract file hash for later PhalanxDB update only
                if magnet_link.startswith('http'):
                    file_param = magnet_link.split('&file=')[-1] if '&file=' in magnet_link else None
                    if file_param:
                        file_hash = f"FILE_HASH_{hashlib.sha1(file_param.encode()).hexdigest()}"

            elif torrent_url:
                try:
                    torrent_hash = _download_and_get_hash(torrent_url)
                except Exception as e:
                    logging.warning(f"Could not extract hash from torrent URL: {e}")
            
            # Check all providers: RD uses PhalanxDB, others use direct API
            from debrid import get_debrid_providers
            all_providers = get_debrid_providers()
            cache_providers = {}
            is_cached = None

            for prov in all_providers:
                prov_result = None
                try:
                    if prov.PROVIDER_NAME == 'Real-Debrid':
                        # RD has no direct cache check API — use PhalanxDB
                        if _phalanx_cache_manager and torrent_hash:
                            rd_status = _phalanx_cache_manager.get_cache_status(torrent_hash)
                            prov_result = rd_status.get('is_cached') if rd_status is not None else None
                        prov_status = 'Yes' if prov_result is True else ('No' if prov_result is False else 'N/A')
                    else:
                        prov_processor = TorrentProcessor(prov)
                        if magnet_link:
                            prov_result = prov_processor.check_cache(magnet_link, remove_cached=True, item=None)
                        elif torrent_url:
                            prov_result = prov_processor.check_cache_for_url(torrent_url, remove_cached=True, item=None)
                        prov_status = 'Yes' if prov_result is True else ('No' if prov_result is False else 'N/A')
                except Exception as _pe:
                    logging.warning(f"[{prov.PROVIDER_NAME}] cache check error: {_pe}")
                    prov_status = 'Error'
                cache_providers[prov.PROVIDER_NAME] = prov_status
                if prov_result is True and is_cached is not True:
                    is_cached = True
                elif prov_result is False and is_cached is None:
                    is_cached = False

            # Update PhalanxDB with primary result
            if cache_manager and torrent_hash:
                try:
                    cache_manager.update_cache_status(torrent_hash, bool(is_cached))
                except Exception as e:
                    logging.error(f"Error updating PhalanxDB cache: {str(e)}")
            if cache_manager and file_hash:
                cache_manager.update_cache_status(file_hash, bool(is_cached))

            # Convert result to the expected format
            if is_cached is True:
                status = 'cached'
            elif is_cached is False:
                status = 'not_cached'
            else:
                status = 'check_unavailable'

            logging.debug(f"Returning cache status for index {index}: {status} providers={cache_providers}")
            return jsonify({'status': status, 'index': index, 'cache_providers': cache_providers}), 200
            
        # Handle multiple hashes (legacy approach)
        hashes = data.get('hashes', [])
        if not hashes:
            return jsonify({'error': 'No hashes provided'}), 400
            
        # Always limit to exactly 5 hashes, preserving the order
        if len(hashes) > 5:
            logging.debug(f"Limiting cache check from {len(hashes)} to exactly 5 hashes")
            hashes = hashes[:5]
            
        # First check PhalanxDB for all hashes at once with multi-status
        cache_status = {}
        hashes_to_check = []
        
        # Get results for all hashes at once
        if cache_manager: # Check cache_manager is not None
            phalanx_results = cache_manager.get_multi_cache_status(hashes)
            for hash_value, status in phalanx_results.items():
                if status is not None:
                    cache_status[hash_value] = 'Yes' if status.get('is_cached') else 'No' # Use .get() for safety
                else:
                    hashes_to_check.append(hash_value)
        else:
             hashes_to_check = hashes # If no cache manager, check all hashes with debrid
                
        if hashes_to_check:
            logging.info(f"Need to check {len(hashes_to_check)} hashes with debrid provider")
            # Get the debrid provider and check its capabilities
            debrid_provider = get_debrid_provider()
            supports_cache_check = debrid_provider.supports_direct_cache_check
            supports_bulk_check = debrid_provider.supports_bulk_cache_checking
            # Derive behavior from capabilities instead of concrete type
            is_real_debrid = getattr(debrid_provider, 'supports_direct_cache_check', False)
            
            if supports_cache_check:
                try:
                    # Optimize for single hash requests
                    if len(hashes_to_check) == 1:
                        hash_value = hashes_to_check[0]
                        is_cached = debrid_provider.is_cached(hash_value)
                        cache_status[hash_value] = 'Yes' if is_cached else 'No'
                        # Update PhalanxDB
                        if cache_manager: # Check cache_manager is not None
                            try:
                                cache_manager.update_cache_status(hash_value, bool(is_cached))
                                logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(is_cached)}")
                            except Exception as e:
                                logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                    elif supports_bulk_check:
                        bulk_result = debrid_provider.is_cached(hashes_to_check)
                        if isinstance(bulk_result, bool):
                            for hash_value in hashes_to_check:
                                cache_status[hash_value] = 'Yes' if bulk_result else 'No'
                                if cache_manager: # Check cache_manager is not None
                                    try:
                                        cache_manager.update_cache_status(hash_value, bool(bulk_result))
                                        logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(bulk_result)}")
                                    except Exception as e:
                                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                        else:
                            for hash_value in hashes_to_check:
                                result = bulk_result.get(hash_value, 'N/A')
                                cache_status[hash_value] = 'Yes' if result is True else 'No' if result is False else 'N/A'
                                if result is not None and result != 'N/A': # Check before updating
                                    if cache_manager: # Check cache_manager is not None
                                        try:
                                            cache_manager.update_cache_status(hash_value, bool(result))
                                            logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(result)}")
                                        except Exception as e:
                                            logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                    else:
                        for hash_value in hashes_to_check:
                            try:
                                is_cached = debrid_provider.is_cached(hash_value)
                                cache_status[hash_value] = 'Yes' if is_cached else 'No'
                                if cache_manager: # Check cache_manager is not None
                                    try:
                                        cache_manager.update_cache_status(hash_value, bool(is_cached))
                                        logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(is_cached)}")
                                    except Exception as e:
                                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                            except Exception as e:
                                logging.error(f"Error checking individual cache status for {hash_value}: {e}")
                                cache_status[hash_value] = 'N/A'
                except Exception as e:
                    logging.error(f"Error checking cache status: {e}")
                    for hash_value in hashes_to_check:
                        cache_status[hash_value] = 'N/A'
            elif is_real_debrid:
                logging.info("Using provider's is_cached method based on capability flags")
                torrent_ids_to_remove = []
                
                for i, hash_value in enumerate(hashes_to_check):
                    try:
                        magnet_link = f"magnet:?xt=urn:btih:{hash_value}"
                        cache_result = debrid_provider.is_cached(
                            magnet_link, 
                            result_title=f"Hash {hash_value}",
                            result_index=i,
                            remove_uncached=True
                        )
                        result_str = 'Yes' if cache_result is True else 'No' if cache_result is False else 'N/A'
                        cache_status[hash_value] = result_str
                        
                        if cache_result is not None and cache_result != 'N/A': # Check before updating
                            if cache_manager: # Check cache_manager is not None
                                try:
                                    cache_manager.update_cache_status(hash_value, bool(cache_result))
                                    logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(cache_result)}")
                                except Exception as e:
                                    logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                        
                        torrent_id = debrid_provider._all_torrent_ids.get(hash_value)
                        if torrent_id:
                            torrent_ids_to_remove.append(torrent_id)
                    except Exception as e:
                        logging.error(f"Error checking cache for hash {hash_value}: {str(e)}")
                        cache_status[hash_value] = 'N/A'
                
                for torrent_id in torrent_ids_to_remove:
                    try:
                        debrid_provider.remove_torrent(torrent_id, "Removed after cache check")
                    except Exception as e:
                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
            else:
                for hash_value in hashes_to_check:
                    cache_status[hash_value] = 'N/A'
                
        # Removed redundant PhalanxDB checks here as they are now handled earlier
        # Ensure PhalanxDB updates only happen if cache_manager exists
        if cache_manager:
            try:
                for hash_value, status_str in cache_status.items():
                    if status_str in ['Yes', 'No']: # Only update if we have a definitive status
                        is_cached = status_str == 'Yes'
                        # Check if this hash already exists in PhalanxDB to avoid redundant updates
                        existing_status = cache_manager.get_cache_status(hash_value)
                        if existing_status is None or existing_status.get('is_cached') != is_cached:
                            cache_manager.update_cache_status(hash_value, is_cached)
                            logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {is_cached}")
            except Exception as e:
                logging.error(f"Error updating PhalanxDB cache after debrid check: {str(e)}")

        return jsonify({'cache_status': cache_status})
    except Exception as e:
        logging.error(f"Error in check_cache_status: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while checking cache status'}), 500

async def _fetch_details_for_id_lookup(id_type: str, media_id: str) -> List[Dict[str, Any]]:
    """
    Helper to fetch media details based on IMDb or TMDb ID.
    Returns a list containing a single result dict if found, else empty list.
    """
    from metadata.metadata import get_metadata
    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)
    results = []
    metadata_result = None
    media_type = None

    def is_better_tv_candidate(metadata):
        """
        Determine if metadata suggests this is better classified as a TV show.
        Returns True if TV indicators are strong, False otherwise.
        """
        if not metadata:
            return False
            
        # Strong TV indicators
        airs_data = metadata.get('airs', {})
        if airs_data and isinstance(airs_data, dict):
            # Has day/time/timezone = TV show
            if airs_data.get('day') or airs_data.get('time') or airs_data.get('timezone'):
                logging.info(f"Strong TV indicator found: airs data = {airs_data}")
                return True
        
        # Runtime analysis (episodes typically 20-70 mins, movies typically 80+ mins)
        runtime = metadata.get('runtime')
        if runtime and isinstance(runtime, int):
            if runtime <= 70:  # Likely episode runtime
                logging.info(f"TV runtime indicator: {runtime} minutes (typical episode length)")
                return True
            elif runtime >= 120:  # Likely movie runtime  
                logging.info(f"Movie runtime indicator: {runtime} minutes (typical movie length)")
                return False
        
        # Check for seasons data
        seasons = metadata.get('seasons', {})
        if seasons and isinstance(seasons, dict) and len(seasons) > 0:
            logging.info(f"Strong TV indicator: seasons data found with {len(seasons)} seasons")
            return True
            
        return False

    def calculate_metadata_quality_score(metadata):
        """Calculate a quality score for metadata completeness."""
        if not metadata:
            return 0
            
        score = 0
        # Basic fields
        if metadata.get('title'): score += 10
        if metadata.get('year'): score += 10
        if metadata.get('genres'): score += 5
        if metadata.get('tmdb_id'): score += 15
        
        # Rich data indicators
        if metadata.get('overview'): score += 5
        if metadata.get('runtime'): score += 5
        if metadata.get('airs'): score += 20  # TV shows have this
        if metadata.get('seasons'): score += 25  # TV shows have this
        if metadata.get('release_date') or metadata.get('first_aired'): score += 10
        
        return score

    try:
        movie_metadata = None
        tv_metadata = None
        
        # Try both movie and TV lookups for IMDb IDs
        if id_type == 'imdb':
            logging.info(f"Trying both movie and TV lookups for IMDb ID: {media_id}")
            
            # Try movie lookup
            try:
                movie_metadata = get_metadata(imdb_id=media_id, item_media_type='movie')
                if movie_metadata:
                    logging.debug(f"Movie metadata found for {media_id}")
            except Exception as e:
                logging.debug(f"Movie lookup failed for {media_id}: {e}")
            
            # Try TV lookup  
            try:
                tv_metadata = get_metadata(imdb_id=media_id, item_media_type='tv')
                if tv_metadata:
                    logging.debug(f"TV metadata found for {media_id}")
            except Exception as e:
                logging.debug(f"TV lookup failed for {media_id}: {e}")
                
        elif id_type == 'tmdb':
            try:
                media_id_int = int(media_id)
                logging.info(f"Trying both movie and TV lookups for TMDB ID: {media_id_int}")
                
                # Try movie lookup
                try:
                    movie_metadata = get_metadata(tmdb_id=media_id_int, item_media_type='movie')
                    if movie_metadata:
                        logging.debug(f"Movie metadata found for TMDB {media_id_int}")
                except Exception as e:
                    logging.debug(f"Movie lookup failed for TMDB {media_id_int}: {e}")
                
                # Try TV lookup
                try:
                    tv_metadata = get_metadata(tmdb_id=media_id_int, item_media_type='tv')
                    if tv_metadata:
                        logging.debug(f"TV metadata found for TMDB {media_id_int}")
                except Exception as e:
                    logging.debug(f"TV lookup failed for TMDB {media_id_int}: {e}")
                    
            except ValueError:
                logging.error(f"Invalid TMDb ID format (after stripping prefix): {media_id}")
                return []

        # Now decide which result to use based on analysis
        if movie_metadata and tv_metadata:
            logging.info(f"Both movie and TV metadata found for {id_type}={media_id}, analyzing to determine best match...")
            
            # Check if TV metadata has strong TV indicators
            if is_better_tv_candidate(tv_metadata):
                logging.info(f"TV metadata has strong indicators, using TV result for {media_id}")
                metadata_result = tv_metadata
                media_type = 'tv'
            elif is_better_tv_candidate(movie_metadata):
                # This shouldn't happen often, but handle edge case
                logging.warning(f"Movie metadata has TV indicators, switching to TV result for {media_id}")
                metadata_result = tv_metadata if tv_metadata else movie_metadata
                media_type = 'tv'
            else:
                # Compare quality scores
                movie_score = calculate_metadata_quality_score(movie_metadata)
                tv_score = calculate_metadata_quality_score(tv_metadata) 
                
                logging.info(f"Quality scores for {media_id}: movie={movie_score}, tv={tv_score}")
                
                if tv_score > movie_score:
                    logging.info(f"TV metadata has higher quality score, using TV result for {media_id}")
                    metadata_result = tv_metadata
                    media_type = 'tv'
                else:
                    logging.info(f"Movie metadata has higher/equal quality score, using movie result for {media_id}")
                    metadata_result = movie_metadata
                    media_type = 'movie'
                    
        elif tv_metadata:
            logging.info(f"Only TV metadata found for {media_id}")
            metadata_result = tv_metadata
            media_type = 'tv'
        elif movie_metadata:
            logging.info(f"Only movie metadata found for {media_id}")
            metadata_result = movie_metadata
            media_type = 'movie'
        else:
            logging.warning(f"No metadata found for {id_type} ID: {media_id}")
            return []

        if metadata_result and media_type:
            tmdb_id = metadata_result.get('tmdb_id')
            title = metadata_result.get('title', 'N/A')
            year = metadata_result.get('year', 'N/A')
            overview = metadata_result.get('overview', '')
            release_date = metadata_result.get('release_date') if media_type == 'movie' else metadata_result.get('first_aired')

            poster_path_final = None # Use a distinct variable name
            genres = []
            vote_average = 0.0
            backdrop_path_full = None

            media_meta_tuple = await asyncio.to_thread(
                get_media_meta, str(tmdb_id), media_type
            )

            if media_meta_tuple:
                 # Use distinct variable for poster path from tuple
                 poster_path_from_meta, _, genres, vote_average, backdrop_path_rel = media_meta_tuple

                 # Assign to final poster path, preferring the one from get_media_meta
                 poster_path_final = poster_path_from_meta

                 if not has_tmdb and (not poster_path_final or 'placeholder' not in poster_path_final):
                     poster_path_final = "static/images/placeholder.png"
                 elif has_tmdb and not poster_path_final:
                      logging.warning(f"Could not retrieve poster for {media_type} {title} (TMDb: {tmdb_id})")
                      poster_path_final = "static/images/placeholder.png"

                 if backdrop_path_rel:
                     backdrop_path_full = f"https://image.tmdb.org/t/p/original{backdrop_path_rel}"
            else:
                 if not has_tmdb:
                      poster_path_final = "static/images/placeholder.png"
                 else:
                      logging.warning(f"get_media_meta failed for {media_type} {title} (TMDb: {tmdb_id}). Using placeholder.")
                      poster_path_final = "static/images/placeholder.png"

            formatted_result = {
                'media_type': 'show' if media_type == 'tv' else media_type, # Normalize tv to show for JS
                'id': str(tmdb_id),
                'title': title,
                'year': year,
                'poster_path': poster_path_final, # Use final path with snake_case
                'overview': overview, # Use corrected overview
                'genres': genres,
                'voteAverage': vote_average,
                'backdrop_path': backdrop_path_full, # Use full URL
                'release_date': release_date, # Use corrected release_date
                'imdb_id': metadata_result.get('imdb_id')
            }
            results.append(formatted_result)
            
            # Log the final decision for debugging
            logging.info(f"Final classification for {id_type}={media_id}: {media_type} - '{title}' ({year})")
        else:
            logging.warning(f"Could not find metadata for {id_type} ID: {media_id}")

    except Exception as e:
        logging.error(f"Error during ID lookup's async helper ({id_type}={media_id}): {e}", exc_info=True)
    return results

@scraper_bp.route('/lookup_by_id', methods=['POST'])
@user_required
@scraper_view_access_required
@onboarding_required
def lookup_by_id(): # This remains synchronous
    id_type = request.form.get('id_type')
    media_id = request.form.get('media_id')

    if not id_type or not media_id or id_type not in ['imdb', 'tmdb']:
        return jsonify({'error': 'Invalid ID type or ID provided'}), 400

    logging.info(f"Performing ID lookup: type={id_type}, id={media_id}")

    results = asyncio.run(_fetch_details_for_id_lookup(id_type, media_id))

    if not results:
        return jsonify({'error': 'Media not found for the provided ID'}), 404

    # --- Simplify result processing - main processing done in helper ---
    processed_results = []
    for item in results: # item already has snake_case keys
        tmdb_id_val = item.get('id')
        db_state = 'Missing'
        if tmdb_id_val:
            try:
                tmdb_id_int = int(tmdb_id_val)
                db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
            except (ValueError, TypeError):
                logging.warning(f"Could not parse tmdb_id_val for db_state check: {tmdb_id_val}")
                db_state = 'Missing'

        item['db_status'] = {
            'Collected': 'collected',
            'Partial': 'partial',
            'Blacklisted': 'blacklisted'
        }.get(db_state, 'processing' if db_state not in ['Missing', 'Ignored', None] else 'missing')

        # Ensure necessary fields are present (redundant check, but safe)
        if 'poster_path' not in item or not item['poster_path']:
             item['poster_path'] = "static/images/placeholder.png"
        if 'year' not in item and item.get('release_date'):
             item['year'] = str(item['release_date'])[:4]
        # Remove inconsistent keys if they somehow slipped through (unlikely now)
        item.pop('mediaType', None)
        item.pop('posterPath', None)
        item.pop('show_overview', None)
        item.pop('backdropPath', None)

        processed_results.append(item)
    # --- End Simplified Processing ---

    logging.info(f"ID lookup successful, returning {len(processed_results)} result(s).")
    return jsonify({'results': processed_results})
