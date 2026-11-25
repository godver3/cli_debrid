"""Symlink folders, NZB file lists and torrent file lists."""

from flask import jsonify, request
import logging
from debrid import get_debrid_provider
from ..models import user_required, scraper_permission_required
from utilities.settings import get_setting, get_all_settings
import os
import time
from utilities.tmdb_cache import get_from_cache, set_in_cache
from ._common import scraper_bp

@scraper_bp.route('/get_symlink_folders')
@user_required
def get_symlink_folders():
    """
    Get available symlink folders for the folder dropdown in torrent modal.
    Returns folder information including:
    - All enabled standard folders from settings (Movies, TV Shows, Anime, Documentary)
    - Custom folders that physically exist in the symlink path
    - Which folders are custom (need /Movies or /TV Shows subfolders)
    - Whether each folder exists on the filesystem yet
    - Folder name settings for auto-selection logic
    """
    try:
        # Check if symlinking is enabled
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Plex')
        if file_management_mode != 'Symlinked/Local':
            return jsonify({'folders': [], 'enabled': False})

        symlink_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

        # Get folder name settings
        folder_settings = {
            'movies_folder_name': get_setting('Debug', 'movies_folder_name', 'Movies'),
            'tv_shows_folder_name': get_setting('Debug', 'tv_shows_folder_name', 'TV Shows'),
            'anime_movies_folder_name': get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies'),
            'anime_tv_shows_folder_name': get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows'),
            'documentary_movies_folder_name': get_setting('Debug', 'documentary_movies_folder_name', 'Documentary Movies'),
            'documentary_tv_shows_folder_name': get_setting('Debug', 'documentary_tv_shows_folder_name', 'Documentary TV Shows'),
            'enable_separate_anime_folders': get_setting('Debug', 'enable_separate_anime_folders', False),
            'enable_separate_documentary_folders': get_setting('Debug', 'enable_separate_documentary_folders', False)
        }

        # Build list of standard folders
        standard_folders = [
            folder_settings['movies_folder_name'],
            folder_settings['tv_shows_folder_name']
        ]

        if folder_settings['enable_separate_anime_folders']:
            standard_folders.extend([
                folder_settings['anime_movies_folder_name'],
                folder_settings['anime_tv_shows_folder_name']
            ])

        if folder_settings['enable_separate_documentary_folders']:
            standard_folders.extend([
                folder_settings['documentary_movies_folder_name'],
                folder_settings['documentary_tv_shows_folder_name']
            ])

        # Get custom folders from content sources
        custom_folders = []
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})
        for source_id, source_config in content_sources.items():
            if isinstance(source_config, dict):
                custom_folder = source_config.get('custom_symlink_subfolder', '').strip()
                if custom_folder and custom_folder not in custom_folders:
                    custom_folders.append(custom_folder)

        # Scan filesystem to see which folders actually exist
        existing_folder_names = []
        if os.path.exists(symlink_path):
            for folder_name in os.listdir(symlink_path):
                folder_full_path = os.path.join(symlink_path, folder_name)
                if os.path.isdir(folder_full_path):
                    existing_folder_names.append(folder_name)

        # Build comprehensive folder list:
        # 1. All enabled standard folders (from settings, regardless of existence)
        # 2. All custom folders (from content source settings, regardless of existence)
        all_folders = []

        # Add standard folders (always include if enabled in settings)
        for folder_name in standard_folders:
            folder_name_lower = folder_name.lower()
            all_folders.append({
                'name': folder_name,
                'is_custom': False,
                'exists': folder_name in existing_folder_names,
                # Add media type permissions based on folder name (for mobile view filtering)
                'allowed_for_movies': 'movie' in folder_name_lower,
                'allowed_for_tv_shows': 'show' in folder_name_lower or 'tv' in folder_name_lower
            })

        # Add ALL custom folders from content source settings (regardless of existence)
        for folder_name in custom_folders:
            all_folders.append({
                'name': folder_name,
                'is_custom': True,
                'exists': folder_name in existing_folder_names,
                # Custom folders work for both movies and TV shows (they have subfolders)
                'allowed_for_movies': True,
                'allowed_for_tv_shows': True
            })

        return jsonify({
            'folders': all_folders,
            'folder_settings': folder_settings,
            'enabled': True
        })

    except Exception as e:
        logging.error(f"Error getting symlink folders: {str(e)}", exc_info=True)
        return jsonify({'error': str(e), 'enabled': False}), 500

@scraper_bp.route('/get_nzb_files', methods=['POST'])
@user_required
@scraper_permission_required
def get_nzb_files():
    """Fetch an NZB URL and return its file list parsed from the XML."""
    try:
        import xml.etree.ElementTree as ET
        from routes.api_tracker import api as _nzb_api

        data = request.json or {}
        nzb_url = data.get('nzb_url', '')
        if not nzb_url:
            return jsonify({'success': False, 'error': 'No NZB URL provided'}), 400

        r = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if r.status_code != 200 or '<nzb' not in r.text.lower():
            return jsonify({'success': False, 'error': f'Failed to fetch NZB (status {r.status_code})'}), 400

        root = ET.fromstring(r.text)
        ns = {'nzb': 'http://www.newzbin.com/DTD/2003/nzb'}

        # Try with and without namespace
        files_el = root.findall('.//file') or root.findall('.//nzb:file', ns)

        files = []
        for f in files_el:
            subject = f.get('subject', '')
            # Extract filename from subject (yEnc format: "filename (part/total)")
            import re as _re
            m = _re.search(r'"([^"]+)"', subject)
            name = m.group(1) if m else subject[:80]
            # Skip par2 files
            if name.lower().endswith('.par2') or '.par2.' in name.lower():
                continue
            # Sum segment sizes
            size_bytes = sum(int(seg.get('bytes', 0)) for seg in f.findall('.//segment') or f.findall('.//nzb:segment', ns))
            size_gb = size_bytes / (1024 ** 3)
            if size_gb >= 0.1:
                size_fmt = f'{size_gb:.2f} GB'
            else:
                size_fmt = f'{size_bytes / (1024 ** 2):.1f} MB'
            files.append({'name': name, 'path': name, 'size': size_bytes, 'size_formatted': size_fmt})

        files.sort(key=lambda x: x['name'])
        return jsonify({'success': True, 'files': files, 'total_files': len(files),
                        'metadata': {'filename': data.get('title', ''), 'hash': '', 'status': 'nzb'}})
    except Exception as e:
        logging.error(f'get_nzb_files error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@scraper_bp.route('/get_torrent_files', methods=['POST'])
@user_required
@scraper_permission_required
def get_torrent_files():
    """
    Get file list from a torrent magnet link.
    Extracts hash from magnet and queries debrid provider for file information.

    Request JSON:
        - magnet: Magnet link
        - torrent_title: Title of torrent (for logging)

    Returns:
        - success: Boolean
        - files: List of file objects with name, size, size_formatted, path
        - total_files: Total number of files
        - torrent_hash: Hash extracted from magnet
        - method: How files were retrieved ('instant_availability' or 'cached' or 'error')
        - error: Error message if failed
    """
    try:
        data = request.json
        magnet = data.get('magnet', '')
        torrent_title = data.get('torrent_title', 'Unknown')

        if not magnet:
            return jsonify({'success': False, 'error': 'No magnet link provided'}), 400

        # Extract hash from magnet link
        from debrid.common import extract_hash_from_magnet
        torrent_hash = extract_hash_from_magnet(magnet)

        if not torrent_hash:
            logging.warning(f"Could not extract hash from magnet for '{torrent_title}'")
            return jsonify({'success': False, 'error': 'Invalid magnet link - could not extract hash'}), 400

        logging.info(f"Getting file list for torrent: '{torrent_title}' (hash: {torrent_hash[:16]}...)")

        # Check cache first (15 minute TTL)
        cache_key = f"torrent_files_{torrent_hash}"
        cached_data = get_from_cache(cache_key)
        if cached_data:
            logging.info(f"Returning cached file list for {torrent_hash[:16]}...")
            cached_data['cached'] = True
            return jsonify(cached_data)

        # Get debrid provider
        debrid_provider = get_debrid_provider()

        files_list = []
        method = 'unknown'
        torrent_info = None  # Store torrent info for metadata

        # Method 1: Try instant availability (preferred - doesn't add torrent)
        try:
            if hasattr(debrid_provider, 'is_cached'):
                logging.debug(f"Checking instant availability for {torrent_hash[:16]}...")

                # For Real-Debrid, is_cached can return file info
                cache_result = debrid_provider.is_cached(magnet)

                # If cached, try to get file info from provider
                if cache_result:
                    logging.debug(f"Torrent is cached, attempting to get file list...")

                    # Try to get torrent info if it exists in user's account
                    if hasattr(debrid_provider, 'get_all_torrents'):
                        try:
                            all_torrents = debrid_provider.get_all_torrents()
                            matching_torrent = None

                            for torrent in all_torrents:
                                if torrent.get('hash', '').lower() == torrent_hash.lower():
                                    matching_torrent = torrent
                                    break

                            if matching_torrent:
                                torrent_id = matching_torrent.get('id')
                                if torrent_id and hasattr(debrid_provider, 'get_torrent_info'):
                                    torrent_info_temp = debrid_provider.get_torrent_info(torrent_id)
                                    if torrent_info_temp and 'files' in torrent_info_temp:
                                        files_list = torrent_info_temp.get('files', [])
                                        torrent_info = torrent_info_temp  # Store for metadata
                                        method = 'existing_torrent'
                                        logging.info(f"Retrieved {len(files_list)} files from existing torrent")
                        except Exception as e:
                            logging.debug(f"Could not retrieve from existing torrents: {e}")
        except Exception as e:
            logging.debug(f"Instant availability check failed: {e}")

        # Method 2: Add torrent temporarily to get file list (fallback)
        if not files_list:
            logging.info(f"Adding torrent temporarily to retrieve file list...")
            try:
                # Resolve HTTP URLs to actual magnet links (for Jackett, etc.)
                from debrid.common.torrent import resolve_to_magnet
                actual_magnet = magnet
                if magnet.startswith('http'):
                    logging.info(f"Resolving HTTP URL to magnet link...")
                    resolved = resolve_to_magnet(magnet)
                    if resolved:
                        actual_magnet = resolved
                        logging.info(f"Resolved to magnet link: {resolved[:60]}...")
                    else:
                        raise Exception("Failed to resolve HTTP URL to magnet link")

                # Use get_torrent_file_list if available — it handles polling for slow providers
                if hasattr(debrid_provider, 'get_torrent_file_list'):
                    result = debrid_provider.get_torrent_file_list(actual_magnet)
                    if result:
                        files_list, _fname, _tid = result
                        method = 'temporary_add'
                        logging.info(f"Retrieved {len(files_list)} files via get_torrent_file_list")
                else:
                    # Fallback: raw add + poll
                    torrent_id = None
                    try:
                        torrent_id = debrid_provider.add_torrent(actual_magnet)
                        if not torrent_id:
                            raise Exception("Failed to add torrent - no ID returned")
                        time.sleep(3)
                        if hasattr(debrid_provider, 'get_torrent_info'):
                            torrent_info_temp = debrid_provider.get_torrent_info(torrent_id)
                            if torrent_info_temp:
                                files_list = torrent_info_temp.get('files', [])
                                torrent_info = torrent_info_temp
                                method = 'temporary_add'
                                logging.info(f"Retrieved {len(files_list)} files from temporarily added torrent")
                    finally:
                        if torrent_id and hasattr(debrid_provider, 'remove_torrent'):
                            try:
                                debrid_provider.remove_torrent(torrent_id, "Temporary file list retrieval")
                            except Exception:
                                pass

            except Exception as e:
                logging.error(f"Error adding torrent temporarily: {e}")
                return jsonify({
                    'success': False,
                    'error': f'Could not retrieve file list: {str(e)}'
                }), 500

        # Process and format file list
        if not files_list:
            return jsonify({
                'success': False,
                'error': 'No files found in torrent'
            }), 404

        # Ensure files_list is a list
        if isinstance(files_list, dict):
            files_list = list(files_list.values())

        # Format file sizes and prepare response
        def format_file_size(bytes_size):
            """Format bytes to human-readable size"""
            try:
                bytes_size = float(bytes_size)
            except (ValueError, TypeError):
                return "0 B"

            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if bytes_size < 1024.0:
                    return f"{bytes_size:.2f} {unit}"
                bytes_size /= 1024.0
            return f"{bytes_size:.2f} PB"

        formatted_files = []
        for file_info in files_list:
            # Handle different provider formats
            file_name = file_info.get('path') or file_info.get('name') or 'Unknown'
            file_size = file_info.get('bytes') or file_info.get('size') or 0

            formatted_files.append({
                'name': os.path.basename(file_name),
                'path': file_name,
                'size': file_size,
                'size_formatted': format_file_size(file_size)
            })

        # Sort by size (largest first)
        formatted_files.sort(key=lambda x: x['size'], reverse=True)

        # Extract metadata from torrent_info if available
        torrent_metadata = {
            'id': torrent_info.get('id') if torrent_info else None,
            'hash': torrent_info.get('hash') if torrent_info else torrent_hash,
            'filename': torrent_info.get('filename') if torrent_info else torrent_title,
            'status': torrent_info.get('status') if torrent_info else 'unknown'
        }

        response_data = {
            'success': True,
            'files': formatted_files,
            'total_files': len(formatted_files),
            'torrent_hash': torrent_hash,
            'method': method,
            'metadata': torrent_metadata
        }

        # Cache the result for 15 minutes
        set_in_cache(cache_key, response_data, ttl_seconds=900)

        logging.info(f"Successfully retrieved {len(formatted_files)} files for '{torrent_title}'")
        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error getting torrent files: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Internal error: {str(e)}'
        }), 500
