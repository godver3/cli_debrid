"""Version propagation and version listing."""

from flask import jsonify, request
from queues.config_manager import load_config
import logging
from routes import admin_required
from datetime import datetime
from ._common import debug_bp

# Columns copied from the original-version row to the new Wanted row. The old
# insert copied only titles, IDs and dates, so propagated items lost genres
# (anime/documentary routing), runtime (bitrate filter), custom folders, tags
# and content-source labels.
_COPY_COLUMNS = (
    'title', 'year', 'type', 'imdb_id', 'tmdb_id', 'episode_title', 'season_number',
    'episode_number', 'airtime', 'release_date', 'physical_release_date',
    'theatrical_release_date', 'theatrical_release_date_checked', 'early_release',
    'no_early_release', 'genres', 'runtime', 'country', 'alternate_title',
    'preferred_alias', 'imdb_aliases', 'title_aliases', 'anime_format', 'requested_season',
    'content_source', 'content_source_detail', 'content_sources', 'source_position',
    'selected_folder', 'selected_folder_is_custom', 'tags', 'fall_back_to_single_scraper',
    'tmdb_collection_id', 'tmdb_collection_name',
)


def _identity_clause(item):
    """WHERE fragment + params that pick out the same movie/episode.

    IDs first: the old title + `year = ?` match never matched a NULL year, so
    items without a year were added again on every run.
    """
    if item['imdb_id']:
        return 'imdb_id = ?', [item['imdb_id']]
    if item['tmdb_id']:
        return 'tmdb_id = ?', [item['tmdb_id']]
    return 'title = ? AND COALESCE(year, -1) = COALESCE(?, -1)', [item['title'], item['year']]


def _propagate(conn, original_version, propagated_version, media_type):
    existing_cols = {r[1] for r in conn.execute('PRAGMA table_info(media_items)').fetchall()}
    cols = [c for c in _COPY_COLUMNS if c in existing_cols]
    query = (f"SELECT {', '.join(cols)} FROM media_items "
             "WHERE REPLACE(version, '*', '') = ? AND (ghostlisted IS NULL OR ghostlisted = 0)"
             if 'ghostlisted' in existing_cols else
             f"SELECT {', '.join(cols)} FROM media_items WHERE REPLACE(version, '*', '') = ?")
    params = [original_version]
    if media_type != 'all':
        query += ' AND type = ?'
        params.append(media_type)
    items = conn.execute(query, params).fetchall()
    logging.info(f"Found {len(items)} items with version {original_version}")

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    insert_cols = cols + ['version', 'state', 'last_updated', 'metadata_updated']
    insert_sql = (f"INSERT INTO media_items ({', '.join(insert_cols)}) "
                  f"VALUES ({', '.join('?' for _ in insert_cols)})")
    added = 0
    for item in items:
        ident, ident_params = _identity_clause(item)
        exists = conn.execute(
            f"SELECT 1 FROM media_items WHERE {ident} AND type = ? "
            "AND COALESCE(season_number, -1) = COALESCE(?, -1) "
            "AND COALESCE(episode_number, -1) = COALESCE(?, -1) "
            "AND REPLACE(version, '*', '') = ? LIMIT 1",
            ident_params + [item['type'], item['season_number'], item['episode_number'],
                            propagated_version]).fetchone()
        if exists:
            continue
        conn.execute(insert_sql, [item[c] for c in cols] + [propagated_version, 'Wanted', now, now])
        added += 1
    return added


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
        if original_version == propagated_version:
            return jsonify({'success': False, 'error': 'Pick two different versions'})

        from database import get_db_connection
        conn = get_db_connection()
        try:
            added_count = _propagate(conn, original_version, propagated_version, media_type)
            conn.commit()
        finally:
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
