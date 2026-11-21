"""Version propagation and version listing."""

from flask import jsonify, request
from queues.config_manager import load_config
import logging
from routes import admin_required
from datetime import datetime
from ._common import debug_bp

@debug_bp.route('/propagate_version', methods=['POST'])
@admin_required
def propagate_version():
    try:
        original_version = request.form.get('original_version', '').strip('*')
        propagated_version = request.form.get('propagated_version', '').strip('*')
        media_type = request.form.get('media_type', 'all')
        
        logging.info(f"Starting version propagation from {original_version} to {propagated_version} for media type: {media_type}")
        
        if not original_version or not propagated_version:
            return jsonify({'success': False, 'error': 'Both versions are required'})
        
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build the base query with media type filter
        base_query = """
            SELECT title, year, type, imdb_id, tmdb_id,
                   episode_title, season_number, episode_number,
                   airtime, release_date
            FROM media_items 
            WHERE REPLACE(version, '*', '') = ?
        """
        
        query_params = [original_version]
        
        if media_type != 'all':
            base_query += " AND type = ?"
            query_params.append(media_type)
            
        cursor.execute(base_query, query_params)
        items = cursor.fetchall()
        
        logging.info(f"Found {len(items)} items with version {original_version}")
        
        # For each item, check if propagated version exists (including asterisk variations)
        added_count = 0
        for item in items:
            cursor.execute("""
                SELECT COUNT(*) 
                FROM media_items 
                WHERE title = ? 
                AND year = ? 
                AND type = ? 
                AND COALESCE(season_number, -1) = COALESCE(?, -1)
                AND COALESCE(episode_number, -1) = COALESCE(?, -1)
                AND REPLACE(version, '*', '') = ?
            """, (
                item['title'], item['year'], item['type'],
                item['season_number'], item['episode_number'],
                propagated_version
            ))
            exists = cursor.fetchone()[0] > 0
            
            if not exists:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.debug(f"Adding {propagated_version} version for {item['title']} ({item['year']}) - " + 
                           (f"S{item['season_number']}E{item['episode_number']}" if item['type'] == 'episode' else 'movie'))
                
                # Add as wanted with propagated version
                cursor.execute("""
                    INSERT INTO media_items (
                        title, year, type, imdb_id, tmdb_id,
                        episode_title, season_number, episode_number,
                        airtime, release_date,
                        version, state, last_updated, metadata_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Wanted', ?, ?)
                """, (
                    item['title'], item['year'], item['type'],
                    item['imdb_id'], item['tmdb_id'],
                    item['episode_title'], item['season_number'], item['episode_number'],
                    item['airtime'], item['release_date'],
                    propagated_version, now, now
                ))
                added_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"Successfully added {added_count} items with version {propagated_version}")
        message = f'Successfully added {added_count} items with version {propagated_version}'
        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        logging.error(f"Error in propagate_version: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

def get_available_versions():
    config = load_config()
    versions = []
    
    # Get versions from Scraping.versions
    scraping_config = config.get('Scraping', {})
    version_configs = scraping_config.get('versions', {})
    
    # Add versions from config
    for version in version_configs.keys():
        clean_version = version.strip('*')
        if clean_version:
            versions.append(clean_version)
    
    # Get versions from the database as backup
    if not versions:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT version FROM media_items WHERE version IS NOT NULL")
        versions = [row['version'].strip('*') for row in cursor.fetchall()]
        conn.close()
    
    return sorted(versions)

@debug_bp.route('/get_versions', methods=['GET'])
@admin_required
def get_versions():
    try:
        versions = get_available_versions()
        return jsonify({'success': True, 'versions': versions})
    except Exception as e:
        logging.error(f"Error getting versions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
