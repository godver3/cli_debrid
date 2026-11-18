"""Filter preset CRUD."""

import logging
import json
import os
from flask import jsonify, request
from routes.models import user_required
from ._common import PRESETS_FILE, discover_bp

# =============================================================================
# Filter Presets API
# =============================================================================

def load_presets():
    """Load filter presets from JSON file"""
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logging.error(f"Error loading presets: {e}")
        return {}

def save_presets(presets):
    """Save filter presets to JSON file"""
    try:
        # Ensure config directory exists
        config_dir = os.path.dirname(PRESETS_FILE)
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)

        with open(PRESETS_FILE, 'w') as f:
            json.dump(presets, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"Error saving presets: {e}")
        return False

@discover_bp.route('/api/presets', methods=['GET'])
@user_required
def get_presets():
    """Get all saved filter presets"""
    try:
        presets = load_presets()
        return jsonify({
            'success': True,
            'presets': presets
        })
    except Exception as e:
        logging.error(f"Error getting presets: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets', methods=['POST'])
@user_required
def save_preset():
    """Save a new filter preset"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Preset name is required'}), 400

        filters = data.get('filters', {})
        if not filters:
            return jsonify({'error': 'No filters to save'}), 400

        # Generate a unique ID for the preset
        import time
        preset_id = f"preset_{int(time.time() * 1000)}"

        # Load existing presets
        presets = load_presets()

        # Add new preset
        presets[preset_id] = {
            'name': name,
            'filters': filters,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
        }

        # Save presets
        if save_presets(presets):
            return jsonify({
                'success': True,
                'preset_id': preset_id,
                'message': f'Preset "{name}" saved successfully'
            })
        else:
            return jsonify({'error': 'Failed to save preset'}), 500

    except Exception as e:
        logging.error(f"Error saving preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['GET'])
@user_required
def get_preset(preset_id):
    """Get a specific filter preset by ID"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        return jsonify({
            'success': True,
            'preset': presets[preset_id]
        })
    except Exception as e:
        logging.error(f"Error getting preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['DELETE'])
@user_required
def delete_preset(preset_id):
    """Delete a filter preset"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        preset_name = presets[preset_id].get('name', preset_id)
        del presets[preset_id]

        if save_presets(presets):
            return jsonify({
                'success': True,
                'message': f'Preset "{preset_name}" deleted successfully'
            })
        else:
            return jsonify({'error': 'Failed to delete preset'}), 500

    except Exception as e:
        logging.error(f"Error deleting preset: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/presets/<preset_id>', methods=['PUT'])
@user_required
def update_preset(preset_id):
    """Update an existing filter preset's filters"""
    try:
        presets = load_presets()

        if preset_id not in presets:
            return jsonify({'error': 'Preset not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        if 'filters' in data:
            presets[preset_id]['filters'] = data['filters']
        if 'name' in data:
            presets[preset_id]['name'] = data['name']

        import time
        presets[preset_id]['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

        if save_presets(presets):
            return jsonify({
                'success': True,
                'message': f'Preset "{presets[preset_id]["name"]}" updated successfully'
            })
        else:
            return jsonify({'error': 'Failed to update preset'}), 500

    except Exception as e:
        logging.error(f"Error updating preset: {e}")
        return jsonify({'error': str(e)}), 500
