from flask import Blueprint, render_template, jsonify, request
from sqlalchemy.orm import selectinload

# Use the cli_battery database context for metadata operations
from cli_battery.app.database import (
    Session as CliSession,
    Item,
    Season,
    init_db,
    DatabaseManager,
)
from cli_battery.app.direct_api import DirectAPI

metadata_bp = Blueprint('metadata', __name__)


@metadata_bp.route('/debug')
def metadata_debug():
    return render_template('metadata_debug.html')


@metadata_bp.route('/api/item/<imdb_id>')
def get_item_by_imdb(imdb_id: str):
    # Ensure DB is initialized
    init_db()
    with CliSession() as session:
        item = (
            session.query(Item)
            .options(
                selectinload(Item.item_metadata),
                selectinload(Item.seasons).selectinload(Season.episodes),
            )
            .filter_by(imdb_id=imdb_id)
            .first()
        )

        if not item:
            # Attempt to auto-create via DirectAPI force refresh
            try:
                api = DirectAPI()
                refreshed_data, source = api.force_refresh_metadata(imdb_id)
            except Exception:
                refreshed_data, source = None, None

            # Re-query after refresh
            item = (
                session.query(Item)
                .options(
                    selectinload(Item.item_metadata),
                    selectinload(Item.seasons).selectinload(Season.episodes),
                )
                .filter_by(imdb_id=imdb_id)
                .first()
            )

            if not item:
                return jsonify({"error": f"No item found for IMDB ID: {imdb_id}", "auto_created": False}), 404

        metadata = {m.key: m.value for m in item.item_metadata}
        seasons = []
        for season in item.seasons:
            season_data = {
                'season': season.season_number,
                'episode_count': season.episode_count,
                'episodes': [],
            }
            if hasattr(season, 'episodes') and season.episodes:
                for episode in sorted(season.episodes, key=lambda ep: ep.episode_number):
                    episode_data = {
                        'episode_number': episode.episode_number,
                        'title': episode.title,
                        'absolute_episode': episode.absolute_episode,
                        'first_aired': episode.first_aired.isoformat() if episode.first_aired else None,
                    }
                    season_data['episodes'].append(episode_data)
            seasons.append(season_data)

        return jsonify(
            {
                "item": {
                    "id": item.id,
                    "imdb_id": item.imdb_id,
                    "title": item.title,
                    "type": item.type,
                    "year": item.year,
                    "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                },
                "metadata": metadata,
                "seasons": seasons,
            }
        )


@metadata_bp.route('/api/delete/<imdb_id>', methods=['POST'])
def delete_item_by_imdb(imdb_id: str):
    # Ensure DB is initialized
    init_db()
    try:
        success = DatabaseManager.delete_item(imdb_id)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


