from flask import render_template, request, jsonify, send_file, redirect, url_for, Blueprint
from app.settings import Settings
from app.metadata_manager import MetadataManager
import io
from app.trakt_auth import TraktAuth
from flask import flash
from sqlalchemy import inspect
from app.database import Session, Item, Metadata, Season, Poster  # Add this line
from app.trakt_metadata import TraktMetadata  # Add this import at the top of the file
import json
import time
import os
from ..logger_config import logger


settings = Settings()

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def home():
    db_stats = MetadataManager.get_stats()
    logger.debug(f"Current staleness_threshold: {settings.staleness_threshold}")
    stats = {
        'total_providers': len(settings.providers),
        'active_providers': sum(1 for provider in settings.providers if provider['enabled']),
        'total_items': db_stats['total_items'],
        'total_metadata': db_stats['total_metadata'],
        'last_update': db_stats['last_update'].strftime('%Y-%m-%d %H:%M:%S') if db_stats['last_update'] else 'N/A',
        'staleness_threshold': f"{settings.staleness_threshold} days"
    }
    logger.debug(f"Stats: {stats}")
    return render_template('home.html', stats=stats)

@main_bp.route('/debug')
def debug():
    search_query = request.args.get('search', '').lower()
    items = MetadataManager.get_all_items()
    
    # Filter items server-side if search query is provided
    if search_query:
        filtered_items = []
        for item in items:
            # Find the year from metadata
            year_metadata = next((m.value for m in item.item_metadata if m.key == 'year'), None)
            item.display_year = year_metadata or item.year
            # Add the actual database updated_at timestamp
            item.db_updated_at = item.updated_at.isoformat() if item.updated_at else None
            
            # Search in basic info
            basic_info = f"{item.title} {item.imdb_id} {item.display_year}".lower()
            if search_query in basic_info:
                filtered_items.append(item)
                continue
                
            # Search in metadata
            for metadata in item.item_metadata:
                metadata_text = f"{metadata.key} {metadata.value}".lower()
                if search_query in metadata_text:
                    filtered_items.append(item)
                    break
        items = filtered_items
    else:
        # If no search query, just set display_year and db_updated_at for all items
        for item in items:
            year_metadata = next((m.value for m in item.item_metadata if m.key == 'year'), None)
            item.display_year = year_metadata or item.year
            # Add the actual database updated_at timestamp
            item.db_updated_at = item.updated_at.isoformat() if item.updated_at else None
    
    # Ensure episodes are loaded with absolute episode numbers for all items
    for item in items:
        if hasattr(item, 'seasons') and item.seasons:
            for season in item.seasons:
                if hasattr(season, 'episodes') and season.episodes:
                    # Sort episodes by episode number to ensure proper display order
                    season.episodes = sorted(season.episodes, key=lambda ep: ep.episode_number)
    
    return render_template('debug.html', items=items)

@main_bp.route('/debug/delete_item/<imdb_id>', methods=['POST'])
def delete_item(imdb_id):
    success = MetadataManager.delete_item(imdb_id)
    return jsonify({"success": success})

@main_bp.route('/settings')
def settings_page():
    return render_template('settings.html', settings=settings.get_all())

@main_bp.route('/debug/schema')
def debug_schema():
    with Session() as session:
        inspector = inspect(session.bind)
        tables = inspector.get_table_names()
        schema = {}
        for table in tables:
            columns = inspector.get_columns(table)
            schema[table] = [{"name": column['name'], "type": str(column['type'])} for column in columns]
        return jsonify(schema)

@main_bp.route('/debug/item/<imdb_id>')
def debug_item(imdb_id):
    settings = Settings()
    if not any(provider['enabled'] for provider in settings.providers):
        return jsonify({"error": "No active metadata provider"}), 400
    
    with Session() as session:
        item = session.query(Item).filter_by(imdb_id=imdb_id).first()
        if not item:
            return jsonify({"error": f"No item found for IMDB ID: {imdb_id}"}), 404
        
        metadata = {m.key: m.value for m in item.item_metadata}
        seasons = []
        for season in item.seasons:
            season_data = {
                'season': season.season_number, 
                'episode_count': season.episode_count,
                'episodes': []
            }
            if hasattr(season, 'episodes') and season.episodes:
                for episode in sorted(season.episodes, key=lambda ep: ep.episode_number):
                    episode_data = {
                        'episode_number': episode.episode_number,
                        'title': episode.title,
                        'absolute_episode': episode.absolute_episode,
                        'first_aired': episode.first_aired.isoformat() if episode.first_aired else None
                    }
                    season_data['episodes'].append(episode_data)
            seasons.append(season_data)
        
        return jsonify({
            "item": {
                "id": item.id,
                "imdb_id": item.imdb_id,
                "title": item.title,
                "type": item.type,
                "year": item.year,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else None
            },
            "metadata": metadata,
            "seasons": seasons
        })

@main_bp.context_processor
def inject_stats():
    stats = MetadataManager.get_stats()
    stats['staleness_threshold'] = f"{settings.staleness_threshold} days"
    logger.debug(f"Injected stats: {stats}")
    return dict(stats=stats)