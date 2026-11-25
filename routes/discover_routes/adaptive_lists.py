"""Adaptive list CRUD."""

import logging
from flask import jsonify, request
from routes.models import user_required
from ._common import discover_bp

# =============================================================================
# Adaptive List API Endpoints
# =============================================================================

@discover_bp.route('/api/adaptive-lists')
@user_required
def get_adaptive_lists():
    """
    Get all configured adaptive lists
    Returns list of adaptive list configurations
    """
    try:
        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        adaptive_lists = []
        for source_id, source_data in content_sources.items():
            if source_id.startswith('Adaptive List'):
                lists = source_data.get('lists', [])
                for idx, list_config in enumerate(lists):
                    adaptive_lists.append({
                        'id': f"{source_id}_{idx}",
                        'source_id': source_id,
                        'index': idx,
                        'name': list_config.get('name', 'Unnamed List'),
                        'media_type': list_config.get('media_type', 'movie'),
                        'filters': list_config.get('filters', {}),
                        'enabled': source_data.get('enabled', False)
                    })

        return jsonify({
            'success': True,
            'lists': adaptive_lists
        })

    except Exception as e:
        logging.error(f"Error getting adaptive lists: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/adaptive-lists', methods=['POST'])
@user_required
def save_adaptive_list():
    """
    Save a new adaptive list as its own content source.
    Each adaptive list is a separate content source with the list name as display_name.
    Expects JSON body with: name, media_type, filters
    """
    try:
        from utilities.settings import get_all_settings, set_setting

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        media_type = data.get('media_type', 'movie')
        filters = data.get('filters', {})

        if not name:
            return jsonify({'error': 'List name is required'}), 400

        if not filters:
            return jsonify({'error': 'At least one filter is required'}), 400

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        # Find next available Adaptive List ID
        # Format is: "Adaptive List_1", "Adaptive List_2", etc.
        existing_ids = [k for k in content_sources.keys() if k.startswith('Adaptive List_')]
        next_num = 1
        if existing_ids:
            nums = []
            for k in existing_ids:
                # Split by underscore: "Adaptive List_1" -> ["Adaptive List", "1"]
                parts = k.split('_')
                if len(parts) >= 2 and parts[-1].isdigit():
                    nums.append(int(parts[-1]))
            if nums:
                next_num = max(nums) + 1

        new_source_id = f'Adaptive List_{next_num}'
        
        # Check if this ID already exists (shouldn't happen, but be safe)
        if new_source_id in content_sources:
            logging.error(f"[Adaptive List] ID {new_source_id} already exists! This shouldn't happen.")
            return jsonify({'error': 'Failed to generate unique ID. Please try again.'}), 500

        # Create the new adaptive list content source
        new_source = {
            'type': 'Adaptive List',
            'enabled': True,
            'display_name': name,
            'media_type': media_type,
            'filters': filters,
            'versions': ['Default'],  # Array format like other content sources
            'allow_specials': False,
            'custom_symlink_subfolder': '',
            'cutoff_date': '',
            'exclude_genres': [],
            'list_length_limit': 0,
            'plex_labels': {
                'enabled': False,
                'label_mode': 'list_name',
                'fixed_label': ''
            }
        }

        # Save the new content source
        set_setting('Content Sources', new_source_id, new_source)

        logging.info(f"[Adaptive List] Created new adaptive list '{name}' as {new_source_id} (type: {media_type})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{name}' created successfully",
            'source_id': new_source_id
        })

    except Exception as e:
        logging.error(f"Error saving adaptive list: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/adaptive-lists/<source_id>', methods=['PUT'])
@user_required
def update_adaptive_list(source_id):
    """
    Update an existing adaptive list content source.
    Expects JSON body with: name, media_type, filters
    """
    try:
        from utilities.settings import get_all_settings, set_setting

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        media_type = data.get('media_type', 'movie')
        filters = data.get('filters', {})

        if not name:
            return jsonify({'error': 'List name is required'}), 400

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        # Update the source - preserve existing settings
        source_config['display_name'] = name
        source_config['media_type'] = media_type
        source_config['filters'] = filters

        # Save the updated settings
        set_setting('Content Sources', source_id, source_config)

        logging.info(f"[Adaptive List] Updated '{name}' ({source_id})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{name}' updated successfully"
        })

    except Exception as e:
        logging.error(f"Error updating adaptive list: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/adaptive-lists/<source_id>', methods=['DELETE'])
@user_required
def delete_adaptive_list(source_id):
    """
    Delete an adaptive list content source
    """
    try:
        from utilities.settings import get_all_settings
        from queues.config_manager import load_config, save_config

        # Get current settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        deleted_name = source_config.get('display_name', source_id)

        # Remove the content source entirely
        del content_sources[source_id]

        # Save the updated content sources
        config = load_config()
        config['Content Sources'] = content_sources
        save_config(config)

        logging.info(f"[Adaptive List] Deleted '{deleted_name}' ({source_id})")

        return jsonify({
            'success': True,
            'message': f"Adaptive list '{deleted_name}' deleted successfully"
        })

    except Exception as e:
        logging.error(f"Error deleting adaptive list: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/adaptive-lists/<source_id>')
@user_required
def get_adaptive_list(source_id):
    """
    Get a specific adaptive list configuration by source_id.
    Used when editing a list in the discover page.
    """
    try:
        from utilities.settings import get_all_settings

        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if source_id not in content_sources:
            return jsonify({'error': 'Adaptive List source not found'}), 404

        source_config = content_sources[source_id]

        # Verify it's an Adaptive List type
        if source_config.get('type') != 'Adaptive List':
            return jsonify({'error': 'Source is not an Adaptive List'}), 400

        return jsonify({
            'success': True,
            'list': {
                'source_id': source_id,
                'name': source_config.get('display_name', ''),
                'media_type': source_config.get('media_type', 'movie'),
                'filters': source_config.get('filters', {})
            }
        })

    except Exception as e:
        logging.error(f"Error getting adaptive list: {e}")
        return jsonify({'error': str(e)}), 500
