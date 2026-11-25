"""Layout CRUD, validation, import/export and defaults."""

from flask import jsonify, request, send_file
from overlays import LayoutValidator
from overlays.element_definitions import get_all_elements_by_category
from overlays.activity_logger import log_activity
from ._common import _get_layout_manager, logger, overlay_bp

# ============================================
# Element Definitions
# ============================================

@overlay_bp.route('/api/overlays/element-definitions', methods=['GET'])
def get_element_definitions():
    """
    Get all available pre-defined element types.

    Returns element definitions grouped by category:
    - basic: Text, Variable Text, Image, SVG, Shapes
    - rating_badges: IMDb, TMDb, Trakt, Rotten Tomatoes
    - quick_badges: Resolution, HDR, Audio, Format, Network, Studio
    """
    try:
        elements = get_all_elements_by_category()
        return jsonify({
            'success': True,
            'elements': elements
        })
    except Exception as e:
        logger.error(f"Failed to get element definitions: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================
# Template CRUD Endpoints
# ============================================

@overlay_bp.route('/api/overlays/layouts', methods=['GET'])
def list_layouts():
    """
    List all overlay templates.

    Query params:
        media_type: Filter by media type (movie, tv, both)
        active_only: Only return active templates (true/false)
    """
    try:
        manager = _get_layout_manager()

        media_type = request.args.get('media_type')
        active_only = request.args.get('active_only', 'false').lower() == 'true'

        templates = manager.list_layouts(media_type=media_type, active_only=active_only)

        return jsonify({
            'success': True,
            'count': len(templates),
            'layouts': templates
        })

    except Exception as e:
        logger.error(f"Failed to list templates: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['GET'])
def get_layout(layout_id):
    """Get a specific template by ID."""
    try:
        manager = _get_layout_manager()
        template = manager.get_layout(layout_id)

        if not template:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        return jsonify({
            'success': True,
            'layout': template
        })

    except Exception as e:
        logger.error(f"Failed to get template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts', methods=['POST'])
def create_layout():
    """
    Create a new template.

    Request body:
        name: Template name (required)
        description: Template description (optional)
        media_type: 'movie', 'tv', or 'both' (required)
        layout_data: Template JSON structure (required)
        is_default: Whether template is active (optional, default: true)
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        # Validate required fields
        required = ['name', 'media_type', 'layout_json']
        missing = [f for f in required if f not in data]
        if missing:
            return jsonify({
                'success': False,
                'error': f'Missing required fields: {", ".join(missing)}'
            }), 400

        manager = _get_layout_manager()
        template = manager.create_layout(
            name=data['name'],
            description=data.get('description', ''),
            media_type=data['media_type'],
            layout_json=data['layout_json'],
            is_default=data.get('is_default', True)
        )

        log_activity('layout_create', title=f"Layout created: \"{template['name']}\" ({template.get('media_type', '')})",
                     stats={'layout_id': template['id'], 'name': template['name'], 'media_type': template.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Template created successfully',
            'layout': template
        }), 201

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to create template: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['PUT'])
def update_layout(layout_id):
    """
    Update an existing template.

    Request body (all optional):
        name: New template name
        description: New description
        layout_data: New template structure
        is_default: New active status
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        manager = _get_layout_manager()
        success = manager.update_layout(
            layout_id,
            name=data.get('name'),
            description=data.get('description'),
            media_type=data.get('media_type'),
            layout_json=data.get('layout_json'),
            is_default=data.get('is_default')
        )

        if not success:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        updated_layout = manager.get_layout(layout_id)
        log_activity('layout_update', title=f"Layout updated: \"{updated_layout['name']}\" ({updated_layout.get('media_type', '')})",
                     stats={'layout_id': layout_id, 'name': updated_layout['name'], 'media_type': updated_layout.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Template updated successfully',
            'layout': updated_layout
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to update template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/<int:layout_id>', methods=['DELETE'])
def delete_layout(layout_id):
    """Delete a template."""
    try:
        manager = _get_layout_manager()
        success = manager.delete_layout(layout_id)

        if not success:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        log_activity('layout_delete', title=f"Layout deleted (id={layout_id})",
                     stats={'layout_id': layout_id})
        return jsonify({
            'success': True,
            'message': 'Template deleted successfully'
        })

    except Exception as e:
        logger.error(f"Failed to delete template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/<int:layout_id>/duplicate', methods=['POST'])
def duplicate_layout(layout_id):
    """Duplicate an existing layout, creating a copy with 'Copy of ' prefix."""
    try:
        manager = _get_layout_manager()
        original = manager.get_layout(layout_id)
        if not original:
            return jsonify({'success': False, 'error': 'Layout not found'}), 404

        import json as _json
        lj = original.get('layout_json')
        if isinstance(lj, str):
            lj = _json.loads(lj)

        new_layout = manager.create_layout(
            name=f"Copy of {original['name']}",
            description=original.get('description', ''),
            media_type=original.get('media_type', 'both'),
            layout_json=lj,
            is_default=False,  # duplicate starts inactive
        )

        log_activity('layout_create', title=f"Layout duplicated: \"{new_layout['name']}\"",
                     stats={'layout_id': new_layout['id'], 'name': new_layout['name'], 'duplicated_from': layout_id})
        return jsonify({
            'success': True,
            'message': 'Layout duplicated successfully',
            'layout': new_layout,
        }), 201

    except Exception as e:
        logger.error(f"Failed to duplicate layout {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/validate', methods=['POST'])
def validate_layout():
    """
    Validate a template structure without saving.

    Request body:
        layout_data: Template JSON to validate
    """
    try:
        data = request.get_json()

        if not data or 'layout_data' not in data:
            return jsonify({'success': False, 'error': 'No layout_data provided'}), 400

        validator = LayoutValidator()
        is_valid, errors = validator.validate_layout(data['layout_data'])
        refs_valid, warnings = validator.validate_layout_references(data['layout_data'])

        return jsonify({
            'success': is_valid,
            'valid': is_valid,
            'errors': errors,
            'warnings': warnings
        })

    except Exception as e:
        logger.error(f"Failed to validate template: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/<int:layout_id>/export', methods=['GET'])
def export_layout(layout_id):
    """Export template to JSON file."""
    try:
        manager = _get_layout_manager()
        template = manager.get_layout(layout_id)

        if not template:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        # Create temp file for export
        import tempfile
        import json

        export_data = {
            'name': template['name'],
            'description': template.get('description', ''),
            'media_type': template.get('media_type', 'both'),
            'layout_json': template['layout_json'],
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(export_data, f, indent=2)
            temp_path = f.name

        filename = f"{template['name'].replace(' ', '_')}.json"

        return send_file(
            temp_path,
            mimetype='application/json',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logger.error(f"Failed to export template {layout_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/import', methods=['POST'])
def import_layout():
    """
    Import layout from JSON body.

    Request body:
        name: Layout name (required)
        description: Layout description (optional)
        media_type: 'movie', 'tv', or 'both' (optional, default: 'both')
        elements: Layout elements array (required)
        is_default: Whether layout should be active (optional, default: true)
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400

        name = data.get('name', 'Imported Layout')
        description = data.get('description', 'Imported layout')
        media_type = data.get('media_type', 'both')
        is_default = data.get('is_default', True)

        # Support both wrapped format {name, layout_json, ...} and raw layout_json blob
        layout_json = data.get('layout_json', data)

        manager = _get_layout_manager()
        template = manager.create_layout(
            name=name,
            description=description,
            media_type=media_type,
            layout_json=layout_json,
            is_default=is_default
        )

        log_activity('layout_create', title=f"Layout imported: \"{template['name']}\" ({template.get('media_type', '')})",
                     stats={'layout_id': template['id'], 'name': template['name'], 'media_type': template.get('media_type')})
        return jsonify({
            'success': True,
            'message': 'Layout imported successfully',
            'layout': template
        }), 201

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to import layout: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@overlay_bp.route('/api/overlays/layouts/load_defaults', methods=['POST'])
def load_default_layouts():
    """
    Import any missing bundled default layouts.
    Layouts whose names already exist in the DB are skipped.
    """
    try:
        manager = _get_layout_manager()
        result = manager.load_default_layouts(skip_existing=True)
        if result['errors']:
            logger.warning(f"load_default_layouts errors: {result['errors']}")
        for name in []:  # log each loaded layout
            pass
        if result['loaded']:
            log_activity('layout_create',
                         title=f"Loaded {result['loaded']} default layout(s)",
                         stats=result)
        return jsonify({
            'success': True,
            'loaded': result['loaded'],
            'skipped': result['skipped'],
            'errors': result['errors'],
        })
    except Exception as e:
        logger.error(f"Failed to load default layouts: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
