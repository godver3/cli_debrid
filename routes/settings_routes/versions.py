"""Version profiles: add, delete, import/export, rename, duplicate, defaults, clear."""

from flask import jsonify, request, render_template, make_response
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from queues.config_manager import get_content_source_settings, get_version_settings
from routes.models import admin_required
import traceback
import json
import os
from database.database_writing import update_version_name
import sys
from ._common import _reload_components_after_settings_change, migrate_clean_stale_scraper_keys_from_versions, migrate_clean_version_filter_keys, settings_bp
from .default_versions import HARDCODED_DEFAULT_VERSIONS

@settings_bp.route('/versions/add', methods=['POST'])
@admin_required
def add_version():
    data = request.json
    version_name = data.get('name')
    if not version_name:
        return jsonify({'success': False, 'error': 'No version name provided'}), 400

    config = load_config()
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping']:
        config['Scraping']['versions'] = {}

    if version_name in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version already exists'}), 400

    # Add the new version with default settings
    config['Scraping']['versions'][version_name] = {
        'enable_hdr': False,
        'max_resolution': '1080p',
        'resolution_wanted': '<=',
        'resolution_weight': 3,
        'hdr_weight': 3,
        'similarity_weight': 3,
        'size_weight': 3,
        'bitrate_weight': 3,
        'min_size_gb': 0.01,
        'max_size_gb': '',
        'min_bitrate_mbps': 0.01,
        'max_bitrate_mbps': '',
        'similarity_threshold': 0.85,
        'similarity_threshold_anime': 0.80,
        'year_match_weight': 3,
        'foreign_language_penalty': 30,
        'audio_description_penalty': 10000,
        'wake_count': None,
        'fallback_version': 'None',
        'anime_filter_mode': 'None',
        'preferred_filter_in': [],
        'preferred_filter_out': [],
        'filter_in': [],
        'filter_out': [],
        'require_physical_release': False,
        'language_code': 'en',
        'enable_spanish_episode_parsing': False,
        'use_alternative_titles': False
    }

    save_config(config)

    # Reload components so the change takes effect mid-run
    _reload_components_after_settings_change('version')

    return jsonify({'success': True, 'version_id': version_name})

@settings_bp.route('/versions/delete', methods=['POST'])
@admin_required
def delete_version():
    data = request.json
    version_id = data.get('version_id')
    target_version_id = data.get('target_version_id') # New: Can be None
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    try:
        # Perform database update first
        from database import get_db_connection
        from database.database_writing import update_version_for_items
        
        conn = get_db_connection()
        updated_count = 0 # Initialize count
        try:
            updated_count = update_version_for_items(version_id, target_version_id)
            logging.info(f"Updated {updated_count} media items in database from version '{version_id}' to '{target_version_id or 'None'}'")
            conn.commit() # Commit the database change
        except Exception as db_error:
            conn.rollback()
            logging.error(f"Database error during version deletion: {db_error}", exc_info=True)
            return jsonify({'success': False, 'error': f'Database error: {db_error}'}), 500
        finally:
            conn.close()

        # Now update the config file
        config = load_config()
        if 'Scraping' in config and 'versions' in config['Scraping'] and version_id in config['Scraping']['versions']:
            # Before deleting, update fallbacks pointing to this version
            versions = config['Scraping']['versions']
            for v_name, v_config in versions.items():
                if v_name != version_id and v_config.get('fallback_version') == version_id:
                    versions[v_name]['fallback_version'] = 'None' # Set to None as requested
                    logging.info(f"Reset fallback_version for version '{v_name}' as '{version_id}' was deleted.")
            
            # --- New Logic: Remove deleted version from Content Sources ---
            content_sources = config.get('Content Sources', {})
            updated_sources_count = 0
            for source_id, source_config in content_sources.items():
                if isinstance(source_config, dict) and 'versions' in source_config:
                    source_versions = source_config['versions']
                    if isinstance(source_versions, dict) and version_id in source_versions:
                        del source_versions[version_id]
                        updated_sources_count += 1
                        logging.info(f"Removed deleted version '{version_id}' from Content Source '{source_id}' (dict format)")
                    elif isinstance(source_versions, list) and version_id in source_versions:
                        source_versions.remove(version_id)
                        updated_sources_count += 1
                        logging.info(f"Removed deleted version '{version_id}' from Content Source '{source_id}' (list format)")
            if updated_sources_count > 0:
                logging.info(f"Removed deleted version from {updated_sources_count} Content Source(s).")

            # Delete the actual version
            del versions[version_id]
            save_config(config) # Save config with updated fallbacks and deleted version

            # Reload components so the change takes effect mid-run
            _reload_components_after_settings_change('version')

            message = f"Version '{version_id}' deleted."
            if updated_count > 0:
                message += f" {updated_count} items reassigned to '{target_version_id or 'versionless'}'."
            return jsonify({'success': True, 'message': message})
        else:
            # This case should ideally not happen if DB update succeeded, but handle defensively
            logging.warning(f"Version '{version_id}' was updated in DB but not found in config for deletion.")
            return jsonify({'success': False, 'error': 'Version not found in config after DB update'}), 404
            
    except Exception as e:
        logging.error(f"Error deleting version '{version_id}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/import_defaults', methods=['POST'])
@admin_required
def import_default_versions():
    default_versions = None
    try:
        # Determine the base path depending on whether the app is frozen
        if getattr(sys, 'frozen', False):
            # If the application is run as a bundle/frozen executable (e.g., PyInstaller)
            base_path = os.path.dirname(sys.executable)
        else:
            # If running as a normal script
            # Use __file__ to get this file (routes/settings_routes/versions.py)
            # Then go up three levels to get the project root
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) 

        default_versions_path = os.path.join(base_path, 'optional_default_versions.json')
        
        try:
            # Read the default versions from the JSON file using the absolute path
            with open(default_versions_path, 'r') as f:
                default_versions = json.load(f)
            logging.info(f"Successfully loaded default versions from: {default_versions_path}")

        except FileNotFoundError:
            logging.warning(f"Default versions file not found at path: {default_versions_path}. Using hardcoded fallback.")
            default_versions = HARDCODED_DEFAULT_VERSIONS
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON in default versions file at path: {default_versions_path}. Using hardcoded fallback.")
            default_versions = HARDCODED_DEFAULT_VERSIONS
        
        if not default_versions or not isinstance(default_versions, dict) or 'versions' not in default_versions:
             # This case handles if the hardcoded version is somehow invalid or the file loaded empty/wrong format
             logging.error("Invalid default versions data (either from file or hardcoded). Cannot import.")
             return jsonify({'success': False, 'error': 'Invalid default versions data format'}), 400
            
        # Load current config
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
            
        # Add each default version with a unique name
        imported_count = 0
        for version_name, version_config in default_versions['versions'].items():
            base_name = version_name
            counter = 1
            new_name = base_name
            
            # Find a unique name for this version
            while new_name in config['Scraping']['versions']:
                new_name = f"{base_name} {counter}"
                counter += 1
                
            config['Scraping']['versions'][new_name] = version_config
            imported_count += 1
            logging.info(f"Imported default version '{version_name}' as '{new_name}'.")
        
        # Save the updated config
        if imported_count > 0:
            save_config(config)
            message = f"Successfully imported {imported_count} default version(s)."
        else:
            message = "No new default versions to import."

        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        logging.error(f"Unexpected error importing default versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'Unexpected error: {str(e)}'}), 500

@settings_bp.route('/versions/export', methods=['GET'])
@admin_required
def export_versions():
    """Export one or all versions as a downloadable JSON file."""
    try:
        version_name = request.args.get('version')  # None = export all
        config = load_config()
        all_versions = config.get('Scraping', {}).get('versions', {})

        if version_name:
            if version_name not in all_versions:
                return jsonify({'success': False, 'error': f"Version '{version_name}' not found"}), 404
            export_data = {'versions': {version_name: all_versions[version_name]}}
            filename = f"version_{version_name.replace(' ', '_')}.json"
        else:
            export_data = {'versions': all_versions}
            filename = 'all_versions.json'

        # json.dumps handles all native Python types; convert float inf to null for JSON compatibility
        def _sanitize(obj):
            if isinstance(obj, float):
                import math
                return None if math.isinf(obj) or math.isnan(obj) else obj
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            return obj

        safe_data = _sanitize(export_data)
        json_str = json.dumps(safe_data, indent=2, ensure_ascii=False)

        response = make_response(json_str)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        logging.error(f"Error exporting versions: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/import', methods=['POST'])
@admin_required
def import_versions():
    """Import versions from an uploaded JSON file.

    conflict_action (form field): 'skip' | 'rename' | 'overwrite'  (default: 'rename')
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'}), 400

        f = request.files['file']
        if not f.filename.endswith('.json'):
            return jsonify({'success': False, 'error': 'File must be a .json file'}), 400

        conflict_action = request.form.get('conflict_action', 'rename')
        if conflict_action not in ('skip', 'rename', 'overwrite'):
            conflict_action = 'rename'

        try:
            raw = json.loads(f.read().decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as je:
            return jsonify({'success': False, 'error': f'Invalid JSON: {je}'}), 400

        if not isinstance(raw, dict) or 'versions' not in raw:
            return jsonify({'success': False, 'error': 'Invalid format — expected {"versions": {...}}'}), 400

        incoming = raw['versions']
        if not isinstance(incoming, dict) or not incoming:
            return jsonify({'success': False, 'error': 'No versions found in file'}), 400

        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}

        existing = config['Scraping']['versions']

        # Backfill missing fields using schema defaults
        from utilities.settings_schema import SETTINGS_SCHEMA
        version_schema = SETTINGS_SCHEMA.get('Scraping', {}).get('versions', {}).get('schema', {})

        def _apply_defaults(version_cfg):
            result = {}
            for field, meta in version_schema.items():
                if field in version_cfg:
                    result[field] = version_cfg[field]
                else:
                    result[field] = meta.get('default')
            # Keep any extra fields not in schema
            for k, v in version_cfg.items():
                if k not in result:
                    result[k] = v
            return result

        imported, skipped, renamed, overwritten = [], [], [], []

        for name, vcfg in incoming.items():
            if not isinstance(vcfg, dict):
                skipped.append(name)
                continue

            filled = _apply_defaults(vcfg)

            if name not in existing:
                existing[name] = filled
                imported.append(name)
            elif conflict_action == 'skip':
                skipped.append(name)
            elif conflict_action == 'overwrite':
                existing[name] = filled
                overwritten.append(name)
            else:  # rename
                base = name
                counter = 1
                new_name = f"{base} (imported)"
                while new_name in existing:
                    new_name = f"{base} (imported {counter})"
                    counter += 1
                existing[new_name] = filled
                renamed.append((name, new_name))
                imported.append(new_name)

        if imported or overwritten:
            save_config(config)

        parts = []
        if imported:
            parts.append(f"Imported: {', '.join(imported)}")
        if overwritten:
            parts.append(f"Overwritten: {', '.join(overwritten)}")
        if renamed:
            parts.append(f"Renamed: {'; '.join(f'{o} → {n}' for o, n in renamed)}")
        if skipped:
            parts.append(f"Skipped: {', '.join(skipped)}")

        message = ' | '.join(parts) if parts else 'Nothing to import.'
        logging.info(f"Version import: {message}")
        return jsonify({'success': True, 'message': message,
                        'imported': imported, 'skipped': skipped,
                        'overwritten': overwritten,
                        'renamed': [{'from': o, 'to': n} for o, n in renamed]})

    except Exception as e:
        logging.error(f"Error importing versions: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/rename', methods=['POST'])
@admin_required
def rename_version():
    data = request.json
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    
    if not old_name or not new_name:
        return jsonify({'success': False, 'error': 'Missing old_name or new_name'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions = config['Scraping']['versions']
        if old_name in versions:
            # Update version name in config
            versions[new_name] = versions.pop(old_name)
            # Don't save yet, more updates needed

            # Update version name in database
            updated_db_count = update_version_name(old_name, new_name)
            logging.info(f"Updated {updated_db_count} media items in database from version prefix '{old_name}' to '{new_name}'")

            # Update fallback_version references in other versions
            for v_name, v_config in versions.items():
                if v_name != new_name and v_config.get('fallback_version') == old_name:
                    versions[v_name]['fallback_version'] = new_name
                    logging.info(f"Updated fallback_version for version '{v_name}' from '{old_name}' to '{new_name}'")

            # --- New Logic: Update Content Source Version Assignments ---
            content_sources = config.get('Content Sources', {})
            updated_sources_count = 0
            for source_id, source_config in content_sources.items():
                if isinstance(source_config, dict) and 'versions' in source_config:
                    source_versions = source_config['versions']
                    
                    # Handle dictionary format {version_name: enabled_boolean}
                    if isinstance(source_versions, dict):
                        if old_name in source_versions:
                            enabled_status = source_versions.pop(old_name) # Remove old, get status
                            source_versions[new_name] = enabled_status # Add new with same status
                            updated_sources_count += 1
                            logging.info(f"Updated version assignment for Content Source '{source_id}': '{old_name}' -> '{new_name}' (dict format)")
                    
                    # Handle list format [version_name, ...] - Less common now but good to support
                    elif isinstance(source_versions, list):
                         if old_name in source_versions:
                             source_versions.remove(old_name)
                             if new_name not in source_versions: # Avoid duplicates
                                 source_versions.append(new_name)
                             updated_sources_count += 1
                             logging.info(f"Updated version assignment for Content Source '{source_id}': '{old_name}' -> '{new_name}' (list format)")
                             
            if updated_sources_count > 0:
                 logging.info(f"Updated version assignments in {updated_sources_count} Content Source(s).")
            # --- End New Logic ---

            # Save config with all updates (version rename, fallbacks, content sources)
            save_config(config)

            # Reload components so the change takes effect mid-run
            _reload_components_after_settings_change('version')

            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Version not found'}), 404
    else:
        # Handle case where Scraping or versions section is missing
        return jsonify({'success': False, 'error': 'Scraping versions configuration not found'}), 404

@settings_bp.route('/versions/duplicate', methods=['POST'])
@admin_required
def duplicate_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' not in config or 'versions' not in config['Scraping'] or version_id not in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

    new_version_id = f"{version_id} Copy"
    counter = 1
    while new_version_id in config['Scraping']['versions']:
        new_version_id = f"{version_id} Copy {counter}"
        counter += 1

    # Create a deep copy of the version settings
    original_settings = config['Scraping']['versions'][version_id]
    new_settings = original_settings.copy()
    
    # Get the version schema to ensure all required fields are present
    version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
    
    # Ensure all required fields are included in the copy
    for field_name, field_schema in version_schema.items():
        if field_name not in new_settings:
            new_settings[field_name] = field_schema['default']

    config['Scraping']['versions'][new_version_id] = new_settings
    config['Scraping']['versions'][new_version_id]['display_name'] = new_version_id

    save_config(config)

    # Reload components so the change takes effect mid-run
    _reload_components_after_settings_change('version')

    return jsonify({'success': True, 'new_version_id': new_version_id})

@settings_bp.route('/scraping/content')
def scraping_content():
    migrate_clean_version_filter_keys()
    migrate_clean_stale_scraper_keys_from_versions()
    config = load_config() # Initial load
    # Log the shape of the config, never its contents — this dumped every
    # API key and token into debug.log on each page load.
    logging.debug(f"[scraping_content] Loaded config sections: {sorted(config.keys())}")
    schema = SETTINGS_SCHEMA
    # Explicitly reload config right before accessing versions
    config = load_config()
    version_names = list(config.get('Scraping', {}).get('versions', {}).keys())
    logging.debug(f"[scraping_content] Extracted version names: {version_names}") # Log extracted names

    # Fill in schema defaults for any missing keys in existing versions
    # so new settings always appear in the UI even for pre-existing versions.
    version_schema = SETTINGS_SCHEMA.get('Scraping', {}).get('versions', {}).get('schema', {})
    _backfill_changed = False
    for _ver_config in config.get('Scraping', {}).get('versions', {}).values():
        if not isinstance(_ver_config, dict):
            continue
        for _field, _fschema in version_schema.items():
            if _field not in _ver_config and 'default' in _fschema:
                _ver_config[_field] = _fschema['default']
                _backfill_changed = True
    if _backfill_changed:
        save_config(config)

    template_name = 'settings_tabs/scraping.html'

    return render_template(template_name,
                           settings=config,
                           settings_schema=schema,
                           version_names=version_names)

@settings_bp.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions_route():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@settings_bp.route('/get_content_source_settings', methods=['GET'])
def get_content_source_settings_route():
    try:
        content_source_settings = get_content_source_settings()
        return jsonify(content_source_settings)
    except Exception as e:
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@settings_bp.route('/get_version_settings')
def get_version_settings_route():
    try:
        version = request.args.get('version')
        if not version:
            return jsonify({'error': 'No version provided'}), 400
        
        version_settings = get_version_settings(version)
        if not version_settings:
            return jsonify({'error': f'No settings found for version: {version}'}), 404
        
        # Ensure max_resolution is included in the settings
        if 'max_resolution' not in version_settings:
            version_settings['max_resolution'] = '1080p'  # or whatever the default should be
        
        return jsonify({version: version_settings})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@settings_bp.route('/save_version_settings', methods=['POST'])
@admin_required
def save_version_settings():
    data = request.json
    version = data.get('version')
    settings = data.get('settings')

    if not version or not settings:
        return jsonify({'success': False, 'error': 'Invalid data provided'}), 400

    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Handle wake_count conversion
        if 'wake_count' in settings:
            if settings['wake_count'] == '' or settings['wake_count'] == 'None' or settings['wake_count'] is None:
                settings['wake_count'] = None
            else:
                try:
                    settings['wake_count'] = int(settings['wake_count'])
                except (ValueError, TypeError):
                    settings['wake_count'] = None
        
        config['Scraping']['versions'][version] = settings
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def build_default_version():
    """Settings for a new 'Default' version, taken from the schema defaults."""
    version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
    return {
        'enable_hdr': version_schema['enable_hdr']['default'],
        'max_resolution': version_schema['max_resolution']['default'],
        'resolution_wanted': version_schema['resolution_wanted']['default'],
        'resolution_weight': version_schema['resolution_weight']['default'],
        'hdr_weight': version_schema['hdr_weight']['default'],
        'similarity_weight': version_schema['similarity_weight']['default'],
        'similarity_threshold': version_schema['similarity_threshold']['default'],
        'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
        'size_weight': version_schema['size_weight']['default'],
        'bitrate_weight': version_schema['bitrate_weight']['default'],
        'year_match_weight': version_schema['year_match_weight']['default'],
        'foreign_language_penalty': version_schema['foreign_language_penalty']['default'],
        'audio_description_penalty': version_schema['audio_description_penalty']['default'],
        'preferred_filter_in': version_schema['preferred_filter_in']['default'],
        'preferred_filter_out': version_schema['preferred_filter_out']['default'],
        'filter_in': version_schema['filter_in']['default'],
        'filter_out': version_schema['filter_out']['default'],
        'min_size_gb': version_schema['min_size_gb']['default'],
        'max_size_gb': version_schema['max_size_gb']['default'],
        'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
        'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
        'wake_count': version_schema['wake_count']['default'],
        'fallback_version': version_schema['fallback_version']['default'],
        'anime_filter_mode': version_schema['anime_filter_mode']['default'],
        'require_physical_release': version_schema['require_physical_release']['default'],
        'language_code': version_schema['language_code']['default']
    }

@settings_bp.route('/versions/add_default', methods=['POST'])
@admin_required
def add_default_version():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        default_version = build_default_version()

        # Add the default version while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find a unique name for the default version
        version_name = 'Default'
        counter = 1
        while version_name in config['Scraping']['versions']:
            version_name = f'Default {counter}'
            counter += 1
            
        config['Scraping']['versions'][version_name] = default_version
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/add_separate_versions', methods=['POST'])
@admin_required
def add_separate_versions():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        # Get the default version settings from the schema
        version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
        base_version = {
            'resolution_wanted': version_schema['resolution_wanted']['default'],
            'resolution_weight': version_schema['resolution_weight']['default'],
            'hdr_weight': version_schema['hdr_weight']['default'],
            'similarity_weight': version_schema['similarity_weight']['default'],
            'similarity_threshold': version_schema['similarity_threshold']['default'],
            'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
            'size_weight': version_schema['size_weight']['default'],
            'bitrate_weight': version_schema['bitrate_weight']['default'],
            'year_match_weight': version_schema['year_match_weight']['default'],
            'foreign_language_penalty': version_schema['foreign_language_penalty']['default'],
            'audio_description_penalty': version_schema['audio_description_penalty']['default'],
            'preferred_filter_in': [],
            'preferred_filter_out': [],
            'filter_in': [],
            'filter_out': [],
            'min_size_gb': version_schema['min_size_gb']['default'],
            'max_size_gb': version_schema['max_size_gb']['default'],
            'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
            'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
            'wake_count': version_schema['wake_count']['default'],
            'fallback_version': version_schema['fallback_version']['default'],
            'anime_filter_mode': version_schema['anime_filter_mode']['default'],
            'require_physical_release': version_schema['require_physical_release']['default'],
            'language_code': version_schema['language_code']['default']
        }

        # Create 1080p version
        version_1080p = base_version.copy()
        version_1080p.update({
            'enable_hdr': False,
            'max_resolution': '1080p',
            'resolution_wanted': '<=', # Explicitly set to <= for 1080p
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Create 4K version
        version_4k = base_version.copy()
        version_4k.update({
            'enable_hdr': True,
            'max_resolution': '2160p',
            'resolution_wanted': '==', # Keep 4K as ==
            'wake_count': 6,
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Add the new versions while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find unique names for the versions
        version_1080p_name = '1080p'
        version_4k_name = '2160p'
        counter_1080p = 1
        counter_4k = 1
        
        while version_1080p_name in config['Scraping']['versions']:
            version_1080p_name = f'1080p {counter_1080p}'
            counter_1080p += 1
            
        while version_4k_name in config['Scraping']['versions']:
            version_4k_name = f'2160p {counter_4k}'
            counter_4k += 1
            
        # Add the new versions
        config['Scraping']['versions'][version_1080p_name] = version_1080p
        config['Scraping']['versions'][version_4k_name] = version_4k
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/clear_all', methods=['POST'])
@admin_required
def clear_all_versions():
    try:
        config = load_config()
        if 'Scraping' in config:
            config['Scraping']['versions'] = {}
            save_config(config)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error clearing versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
