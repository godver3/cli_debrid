from flask import jsonify, Blueprint, request
from app.settings import Settings
from app.metadata_manager import MetadataManager
from app.logger_config import logger
from app.database import DatabaseManager  # Add this import
import json

settings = Settings()

api_bp = Blueprint('api', __name__)

@api_bp.route('/api/movie/metadata/<imdb_id>', methods=['GET'])
def get_movie_metadata(imdb_id):
    try:
        print(f"Fetching movie metadata for IMDB ID: {imdb_id}")
        metadata, source = MetadataManager.get_movie_metadata(imdb_id)
        if metadata:
            print(f"Successfully retrieved movie metadata for IMDB ID: {imdb_id} from {source}")
            return jsonify({"data": metadata, "source": source})
        else:
            logger.warning(f"Movie metadata not found for IMDB ID: {imdb_id}")
            return jsonify({"error": "Movie metadata not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching movie metadata: {str(e)}")
        return jsonify({"error": str(e)}), 500

@api_bp.route('/api/movie/release_dates/<imdb_id>', methods=['GET'])
def get_movie_release_dates(imdb_id):
    try:
        print(f"Fetching movie release dates for IMDB ID: {imdb_id}")
        release_dates = MetadataManager.get_release_dates(imdb_id)
        if release_dates:
            print(f"Successfully retrieved movie release dates for IMDB ID: {imdb_id}")
            return jsonify(release_dates)
        else:
            logger.warning(f"Movie release dates not found for IMDB ID: {imdb_id}")
            return jsonify({"error": "Movie release dates not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching movie release dates: {str(e)}")
        return jsonify({"error": str(e)}), 500

@api_bp.route('/api/show/metadata/<imdb_id>', methods=['GET'])
def get_show_metadata(imdb_id):
    try:
        print(f"Fetching show metadata for IMDB ID: {imdb_id}")
        metadata, source = MetadataManager.get_show_metadata(imdb_id)
        if metadata:
            # Ensure all values are JSON strings
            processed_metadata = {}
            for key, value in metadata.items():
                if isinstance(value, (dict, list)):
                    processed_metadata[key] = json.dumps(value)
                elif not isinstance(value, str):
                    processed_metadata[key] = json.dumps(value)
                else:
                    processed_metadata[key] = value

            print(f"Successfully retrieved show metadata for IMDB ID: {imdb_id} from {source}")
            return jsonify({"data": processed_metadata, "source": source})
        else:
            logger.warning(f"Show metadata not found for IMDB ID: {imdb_id}")
            return jsonify({"error": "Show metadata not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching show metadata: {str(e)}")
        return jsonify({"error": str(e)}), 500

@api_bp.route('/api/show/seasons/<imdb_id>', methods=['GET'])
def get_show_seasons(imdb_id):
    try:
        print(f"Fetching seasons for IMDB ID: {imdb_id}")
        seasons = MetadataManager.get_seasons(imdb_id)
        if seasons:
            print(f"Successfully retrieved seasons for IMDB ID: {imdb_id}")
            return jsonify(seasons)
        else:
            logger.warning(f"Seasons not found for IMDB ID: {imdb_id}")
            return jsonify({"error": "Seasons not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching seasons: {str(e)}")
        return jsonify({"error": str(e)}), 500

@api_bp.route('/api/tmdb_to_imdb/<tmdb_id>', methods=['GET'])
def tmdb_to_imdb(tmdb_id):
    try:
        print(f"Converting TMDB ID to IMDB ID: {tmdb_id}")
        imdb_id = MetadataManager.tmdb_to_imdb(tmdb_id)
        
        if imdb_id:
            print(f"Successfully converted TMDB ID {tmdb_id} to IMDB ID {imdb_id}")
            return jsonify({"imdb_id": imdb_id})
        else:
            logger.warning(f"No IMDB ID found for TMDB ID: {tmdb_id}")
            return jsonify({"error": f"No IMDB ID found for TMDB ID: {tmdb_id}"}), 404
    except Exception as e:
        logger.error(f"Error in tmdb_to_imdb conversion: {str(e)}", exc_info=True)
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@api_bp.route('/api/tmdb_to_imdb/<tmdb_id>/force_refresh', methods=['POST'])
def force_refresh_tmdb_mapping(tmdb_id):
    """Force refresh a TMDB to IMDB mapping, regardless of staleness."""
    try:
        from flask import request
        media_type = request.json.get('media_type') if request.is_json else None
        
        logger.info(f"Force refreshing TMDB mapping for {tmdb_id} (media_type: {media_type})")
        imdb_id, source = MetadataManager.force_refresh_tmdb_mapping(tmdb_id, media_type=media_type)
        
        if imdb_id:
            logger.info(f"Successfully force refreshed TMDB ID {tmdb_id} to IMDB ID {imdb_id} (source: {source})")
            return jsonify({
                "imdb_id": imdb_id,
                "source": source,
                "message": "Mapping force refreshed successfully"
            })
        else:
            logger.warning(f"No IMDB ID found for TMDB ID: {tmdb_id} after force refresh")
            return jsonify({"error": f"No IMDB ID found for TMDB ID: {tmdb_id}"}), 404
    except Exception as e:
        logger.error(f"Error in force_refresh_tmdb_mapping for {tmdb_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@api_bp.route('/api/debug/delete_all_items', methods=['POST'])
def delete_all_items():
    try:
        success = DatabaseManager.delete_all_items()
        if success:
            logger.info("Successfully deleted all items")
            return jsonify({"success": True, "message": "All items deleted successfully"})
        else:
            logger.warning("Failed to delete all items")
            return jsonify({"success": False, "error": "Failed to delete all items"}), 500
    except Exception as e:
        logger.error(f"Error deleting all items: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@api_bp.route('/api/stats', methods=['GET'])
def get_stats():
    # Create a new settings instance to ensure fresh values
    current_settings = Settings()
    db_stats = MetadataManager.get_stats()
    stats = {
        'total_items': db_stats['total_items'],
        'total_metadata': db_stats['total_metadata'],
        'last_update': db_stats['last_update'].strftime('%Y-%m-%d %H:%M:%S') if db_stats['last_update'] else 'N/A',
        'staleness_threshold': f"{current_settings.staleness_threshold} days"
    }
    logger.debug(f"Current staleness threshold: {current_settings.staleness_threshold}")
    return jsonify(stats)
