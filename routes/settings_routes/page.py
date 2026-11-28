"""Settings page, program settings, full config, settings save, single-setting save."""

from flask import jsonify, request, render_template, Response, current_app
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from queues.config_manager import clean_notifications
from routes.models import admin_required, onboarding_required, user_required
import traceback
import json
import os
import platform
import threading
from ._common import settings_bp
from .versions import get_content_source_settings_route

def _builtin_mount_debrid(config):
    """The built-in mount serves the debrid files (shows Streaming only)."""
    try:
        from utilities import builtin_engine
        return builtin_engine.selected(config)
    except Exception:
        return False


def _preset_collection_groups():
    try:
        from database.plex_preset_collections import catalog_for_template
        return catalog_for_template()
    except Exception as e:
        logging.error(f"Could not load preset collection catalog: {e}")
        return []


@settings_bp.route('/', methods=['GET'])
@admin_required
@onboarding_required
def index():
    try:
        config = load_config()
        config = clean_notifications(config)
        
        # --- BEGIN Auto-fix for missing Content Source type ---
        if 'Content Sources' in config and isinstance(config['Content Sources'], dict):
            content_sources = config['Content Sources']
            valid_source_types = SETTINGS_SCHEMA.get('Content Sources', {}).get('schema', {}).keys()
            fixed_count = 0
            
            for source_id, source_config in list(content_sources.items()): # Use list() for safe iteration if modifying
                if isinstance(source_config, dict):
                    # Check if type is missing or empty
                    if not source_config.get('type'): 
                        # Try to infer type from source_id (e.g., "Overseerr_1")
                        parts = source_id.split('_')
                        if parts:
                            potential_type = parts[0]
                            # Check if the inferred type is valid according to the schema
                            if potential_type in valid_source_types:
                                source_config['type'] = potential_type
                                fixed_count += 1
                                logging.warning(f"Auto-fixed missing 'type' for Content Source '{source_id}'. Set type to '{potential_type}'.")
                            else:
                                logging.error(f"Could not auto-fix Content Source '{source_id}': Inferred type '{potential_type}' is not valid.")
                        else:
                            logging.error(f"Could not auto-fix Content Source '{source_id}': Cannot infer type from ID format.")
                else:
                     # Handle cases where the source config itself isn't a dictionary (less common)
                     logging.error(f"Invalid configuration for Content Source '{source_id}': Expected a dictionary, found {type(source_config)}. Please check settings file.")
                     # Optionally remove the invalid entry: del content_sources[source_id]
            
            if fixed_count > 0:
                 logging.info(f"Automatically corrected the 'type' field for {fixed_count} Content Source(s) for rendering.")
                 # Decide whether to save the fixes automatically:
                 # save_config(config) # Uncomment this line to persist the fixes immediately
                 # Keeping it commented means fixes are only for this page load, 
                 # user needs to save settings via UI to persist.
                 
        # --- END Auto-fix ---

        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
        source_types_from_schema = list(SETTINGS_SCHEMA["Content Sources"]["schema"].keys())
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}
        is_windows = platform.system() == 'Windows'
        content_source_settings_response = get_content_source_settings_route()
        scraping_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        logging.debug(f"[index route] Extracted scraping versions: {scraping_versions}")

        # Ensure 'Scrapers' exists in the config
        if 'Scrapers' not in config:
            config['Scrapers'] = {}
        
        # Only keep the scrapers that are actually configured
        configured_scrapers = {}
        for scraper, scraper_config in config['Scrapers'].items():
            scraper_type = scraper.split('_')[0]  # Assuming format like 'Zilean_1'
            # For custom-named scrapers (e.g. Newznab with user-given name), check the type field
            if scraper_type not in scraper_settings and isinstance(scraper_config, dict):
                scraper_type = scraper_config.get('type', scraper_type)
            if scraper_type in scraper_settings:
                configured_scrapers[scraper] = scraper_config

        config['Scrapers'] = configured_scrapers

        # Ensure 'UI Settings' exists in the config
        if 'UI Settings' not in config:
            config['UI Settings'] = {}

        
        # Ensure 'enable_user_system' exists in 'UI Settings'
        if 'enable_user_system' not in config['UI Settings']:
            config['UI Settings']['enable_user_system'] = True  # Default to True
        
        
        # Ensure 'Content Sources' exists in the config
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Ensure each content source is a dictionary
        for source, source_config in config['Content Sources'].items():
            if not isinstance(source_config, dict):
                # Log again or handle as needed if auto-fix didn't remove it
                logging.warning(f"Content source '{source}' is not a dictionary after potential auto-fix attempt.")
                config['Content Sources'][source] = {} # Replace with empty dict to prevent template errors

        # Initialize notification_settings
        if 'Notifications' not in config:
            config['Notifications'] = {
                'Telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
                'Discord': {'enabled': False, 'webhook_url': ''},
                'NTFY': {'enabled': False, 'host': '', 'topic': '', 'api_key': '', 'priority': ''},
                'Email': {
                    'enabled': False,
                    'smtp_server': '',
                    'smtp_port': 587,
                    'smtp_username': '',
                    'smtp_password': '',
                    'from_address': '',
                    'to_address': ''
                }
            }

        # Determine if Windows symlinks are allowed
        allow_windows_symlinks_value = config.get('Debug', {}).get('use_symlinks_on_windows', False)

        # --- Add logging before rendering ---
        overseer_source_check_route = config.get('Content Sources', {}).get('Overseerr_1', 'Overseerr_1 not found')
        logging.debug(f"[Route /] State of 'Overseerr_1' before passing to template: {json.dumps(overseer_source_check_route, indent=2)}")
        # ------------------------------------

        content_source_settings = content_source_settings_response.get_json() if isinstance(content_source_settings_response, Response) else content_source_settings_response

        # Get environment mode from environment variable
        environment_mode = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full')

        template_name = 'settings.html'

        # Debug logging

        # Create response with template
        response = current_app.make_response(render_template(template_name,
                               settings=config,
                               notification_settings=config['Notifications'],
                               scraper_types=scraper_types,
                               scraper_settings=scraper_settings,
                               source_types=source_types_from_schema,
                               content_source_settings=content_source_settings,
                               version_names=scraping_versions,
                               settings_schema=SETTINGS_SCHEMA,
                               preset_collection_groups=_preset_collection_groups(),
                               builtin_mount_debrid=_builtin_mount_debrid(config),
                               is_windows=is_windows,
                               allow_windows_symlinks=allow_windows_symlinks_value,
                               environment_mode=environment_mode))

        # Disable caching for settings page to ensure fresh data
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'

        # Disable Flask-Compress for this response by removing Accept-Encoding handling
        # This tells Flask-Compress to skip compression for this specific response
        if 'Content-Encoding' not in response.headers:
            response.headers['Content-Length'] = str(len(response.get_data()))

        return response
    except Exception as e:
        current_app.logger.error(f"Error in settings route: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return render_template('error.html', error_message="An error occurred while loading settings."), 500

@settings_bp.route('/api/program_settings', methods=['GET'])
@admin_required
def api_program_settings():
    try:
        config = load_config()
        
        def merge_defaults(config_section, schema_section):
            if not isinstance(schema_section, dict):
                return config_section
                
            result = config_section.copy() if config_section else {}
            
            # Handle schema sections with explicit type and default
            if 'type' in schema_section and 'default' in schema_section:
                if not config_section:  # If no user value, use default
                    return schema_section['default']
            
            # Handle nested schema sections
            for key, schema_value in schema_section.items():
                if isinstance(schema_value, dict):
                    if 'default' in schema_value and key not in result:
                        result[key] = schema_value['default']
                    elif 'schema' in schema_value:
                        if key not in result:
                            result[key] = {}
                        result[key] = merge_defaults(result.get(key, {}), schema_value['schema'])
            
            return result

        # Merge defaults for each section
        program_settings = {}
        sections_to_include = ['Scrapers', 'Content Sources', 'Debug', 'Plex', 'Debrid Provider', 'Usenet Provider']
        
        for section in sections_to_include:
            if section in SETTINGS_SCHEMA:
                program_settings[section] = merge_defaults(
                    config.get(section, {}),
                    SETTINGS_SCHEMA[section]
                )
            else:
                program_settings[section] = config.get(section, {})

        return jsonify(program_settings)
    except Exception as e:
        logging.error(f"Error in api_program_settings: {str(e)}", exc_info=True)
        return jsonify({"error": "An error occurred while loading program settings."}), 500

@settings_bp.route('/scraping/get')
def get_scraping_settings():
    config = load_config()
    scraping_settings = config.get('Scraping', {})
    return jsonify(scraping_settings)

@settings_bp.route('/api/config', methods=['GET'])
@user_required
def get_full_config():
    """Get full configuration for modal/API access"""
    import math

    def sanitize(obj):
        """Recursively replace non-JSON-serializable floats with None."""
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sanitize(v) for v in obj]
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return None
        return obj

    try:
        config = load_config()
        return jsonify(sanitize(config))
    except Exception as e:
        logging.error(f"Error loading config: {str(e)}", exc_info=True)
        return jsonify({"error": "Failed to load configuration"}), 500

@settings_bp.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    try:
        new_settings = request.json
        config = load_config()

        # Detect if settings requiring restart are being changed
        requires_restart = False
        restart_reasons = []

        # Check for Content Sources changes
        if 'Content Sources' in new_settings:
            # Poster-only fields are read live on every sync — no restart needed
            _POSTER_ONLY_KEYS = {'poster_design', 'poster_accent', 'poster_eyebrow', 'poster_icon',
                                 'collection_name_movies', 'collection_name_shows'}
            _old_sources = config.get('Content Sources', {})
            _new_sources = new_settings['Content Sources']
            _needs_restart = False
            for src_id, new_src in _new_sources.items():
                old_src = _old_sources.get(src_id, {})
                if not isinstance(new_src, dict) or not isinstance(old_src, dict):
                    _needs_restart = True
                    break
                # Check plex_collection sub-dict
                old_pc = old_src.get('plex_collection', {}) if isinstance(old_src, dict) else {}
                new_pc = new_src.get('plex_collection', {}) if isinstance(new_src, dict) else {}
                # Check non-plex_collection keys
                old_rest = {k: v for k, v in old_src.items() if k != 'plex_collection'}
                new_rest = {k: v for k, v in new_src.items() if k != 'plex_collection'}
                if old_rest != new_rest:
                    _needs_restart = True
                    break
                # Check plex_collection keys — only poster-only keys changed?
                all_pc_keys = set(old_pc.keys()) | set(new_pc.keys())
                non_poster_changed = any(
                    k not in _POSTER_ONLY_KEYS and old_pc.get(k) != new_pc.get(k)
                    for k in all_pc_keys
                )
                if non_poster_changed:
                    _needs_restart = True
                    break
            # Also restart if sources were added or removed
            if set(_old_sources.keys()) != set(_new_sources.keys()):
                _needs_restart = True
            if _needs_restart:
                requires_restart = True
                restart_reasons.append('Content Sources')
            else:
                logging.info("Content Sources: only poster settings changed — skipping restart")

        # Check for Debrid Provider changes
        if 'Debrid Provider' in new_settings:
            debrid_settings = new_settings['Debrid Provider']
            # Parse fallback_providers JSON string if sent as string from hidden input
            import json as _json
            if 'fallback_providers' in debrid_settings and isinstance(debrid_settings['fallback_providers'], str):
                try:
                    debrid_settings['fallback_providers'] = _json.loads(debrid_settings['fallback_providers'])
                except Exception:
                    debrid_settings['fallback_providers'] = []
            # Check if actual values changed, not just presence
            current_debrid = config.get('Debrid Provider', {})
            for key, new_value in debrid_settings.items():
                current_value = current_debrid.get(key)
                if new_value != current_value:
                    requires_restart = True
                    restart_reasons.append('Debrid Provider')
                    break

        if requires_restart:
            logging.info(f"Settings requiring restart detected: {', '.join(set(restart_reasons))}")

        # Detect if ONLY UI settings are being changed (no restart/reload needed)
        # Compare new settings with current config to find what actually changed
        # Read live on every run, so saving them needs no restart
        ui_only_categories = {'Library Manager', 'Discover Settings', 'Overlay Settings', 'AI Assistant',
                              'Plex Preset Collections'}
        actually_changed_categories = set()

        for category, values in new_settings.items():
            current_values = config.get(category, {})
            if values != current_values:
                actually_changed_categories.add(category)
                logging.debug(f"Detected change in category: {category}")

        # Skip restart if: (1) nothing changed OR (2) only UI settings changed
        skip_restart = (len(actually_changed_categories) == 0 or
                       actually_changed_categories.issubset(ui_only_categories))

        logging.info("Received settings update request.")

        if skip_restart:
            if len(actually_changed_categories) == 0:
                logging.info("No settings changed - will skip restart")
            else:
                logging.info(f"UI-only settings change detected ({', '.join(actually_changed_categories)}) - will skip restart")
        else:
            logging.info(f"Settings requiring full reinitialization: {', '.join(actually_changed_categories)}")
        # Optional: Log specific sections if needed for debugging
        # logging.info(f"File Management: {json.dumps(new_settings.get('File Management', {}), indent=2)}")
        # logging.info(f"Plex: {json.dumps(new_settings.get('Plex', {}), indent=2)}")
        # logging.info(f"Staleness Threshold section: {json.dumps(new_settings.get('Staleness Threshold', {}), indent=2)}")

        # --- REMOVED BLOCK ---
        # The block that directly modified settings.json for staleness_threshold
        # has been removed. The value should be handled by the main config update below.
        # --- END REMOVED BLOCK ---

        # Process Plex library strings if they are being updated
        if 'Plex' in new_settings:
            plex_updates = new_settings['Plex']
            if 'movie_libraries' in plex_updates and isinstance(plex_updates['movie_libraries'], str):
                raw_movie_libraries = plex_updates['movie_libraries']
                plex_updates['movie_libraries'] = ','.join([lib.strip() for lib in raw_movie_libraries.split(',') if lib.strip()]) if raw_movie_libraries else ''
            if 'shows_libraries' in plex_updates and isinstance(plex_updates['shows_libraries'], str):
                raw_shows_libraries = plex_updates['shows_libraries']
                plex_updates['shows_libraries'] = ','.join([lib.strip() for lib in raw_shows_libraries.split(',') if lib.strip()]) if raw_shows_libraries else ''


        # Validate Plex libraries ONLY if user is actively changing to Plex mode or changing Plex library settings
        # Don't validate if they're just saving other settings and Plex happens to be the current mode
        should_validate_plex = False

        if 'File Management' in new_settings:
            new_file_mgmt = new_settings.get('File Management', {})
            old_file_mgmt = config.get('File Management', {})

            new_mode = new_file_mgmt.get('file_collection_management', '')
            old_mode = old_file_mgmt.get('file_collection_management', '')

            logging.info(f"DEBUG Plex validation - Old mode: '{old_mode}', New mode: '{new_mode}'")

            # Check if user is changing TO Plex mode (not already on Plex)
            if new_mode == 'Plex' and old_mode != 'Plex':
                logging.info("DEBUG User is switching TO Direct Mount - validation will be required")
                should_validate_plex = True

        # Check if Plex library settings are being actively changed
        if 'Plex' in new_settings:
            new_plex = new_settings.get('Plex', {})
            old_plex = config.get('Plex', {})

            # Handle None values properly
            old_movie_libs = (old_plex.get('movie_libraries') or '').strip()
            old_show_libs = (old_plex.get('shows_libraries') or '').strip()
            new_movie_libs = (new_plex.get('movie_libraries') or '').strip()
            new_show_libs = (new_plex.get('shows_libraries') or '').strip()

            logging.info(f"DEBUG Plex libraries - Old: movie='{old_movie_libs}', show='{old_show_libs}' | New: movie='{new_movie_libs}', show='{new_show_libs}'")

            if new_movie_libs != old_movie_libs or new_show_libs != old_show_libs:
                # Only validate if we're actually setting new values (not clearing them accidentally)
                if new_movie_libs or new_show_libs:
                    logging.info("DEBUG Plex libraries changed to non-empty values - validation will be required")
                    should_validate_plex = True
                else:
                    logging.info("DEBUG Plex libraries changed to empty (probably from different settings page) - skipping validation and restoring old values")
                    # Restore old values to prevent accidental clearing
                    new_settings['Plex']['movie_libraries'] = old_movie_libs
                    new_settings['Plex']['shows_libraries'] = old_show_libs

        logging.info(f"DEBUG Should validate Plex: {should_validate_plex}")

        # Only validate if we detected a relevant change
        if should_validate_plex:
            file_management = new_settings.get('File Management', config.get('File Management', {}))
            if (file_management.get('file_collection_management') == 'Plex'
                    and (file_management.get('plex_mode_media_server') or 'plex') not in ('silo', 'jellyfin')):
                plex_settings_for_validation = new_settings.get('Plex', config.get('Plex', {}))
                movie_libraries_for_validation = (plex_settings_for_validation.get('movie_libraries') or '').strip()
                show_libraries_for_validation = (plex_settings_for_validation.get('shows_libraries') or '').strip()

                logging.info(f"Validating Plex libraries - Movie: '{movie_libraries_for_validation}', Shows: '{show_libraries_for_validation}'")

                if not movie_libraries_for_validation or not show_libraries_for_validation:
                    error_msg = "When using Plex as your library management system, you must specify both a movie library and a TV show library."
                    logging.error(f"Settings validation failed: {error_msg}")
                    return jsonify({
                        "status": "error",
                        "message": error_msg
                    }), 400
        else:
            logging.info("Plex validation skipped - no relevant changes detected")

        # Handle mutual exclusivity between Plex and Jellyfin/Emby settings
        # Check the Media Server Type to determine which settings to keep
        file_management_settings = new_settings.get('File Management', {})
        media_server_type = file_management_settings.get('media_server_type', '')
        
        # Check if Jellyfin/Emby settings are being updated
        debug_settings = new_settings.get('Debug', {})
        if 'emby_jellyfin_url' in debug_settings and debug_settings['emby_jellyfin_url'].strip():
            # Jellyfin/Emby URL is being set
            if media_server_type == 'jellyfin':
                # User has selected Jellyfin/Emby, clear Plex settings
                logging.info("Jellyfin/Emby URL detected and Media Server Type is Jellyfin, clearing Plex settings")
                
                # Clear Plex settings in the new_settings to prevent them from being saved
                if 'Plex' in new_settings:
                    new_settings['Plex']['url'] = ''
                    new_settings['Plex']['token'] = ''
                
                # Clear File Management Plex settings
                if 'File Management' in new_settings:
                    new_settings['File Management']['plex_url_for_symlink'] = ''
                    new_settings['File Management']['plex_token_for_symlink'] = ''
                
                # Also clear these settings in the current config to ensure they're cleared
                if 'Plex' not in config:
                    config['Plex'] = {}
                config['Plex']['url'] = ''
                config['Plex']['token'] = ''
                
                if 'File Management' not in config:
                    config['File Management'] = {}
                config['File Management']['plex_url_for_symlink'] = ''
                config['File Management']['plex_token_for_symlink'] = ''
            else:
                logging.info("Jellyfin/Emby URL detected but Media Server Type is not Jellyfin, keeping Plex settings")
            
        # Check if Plex settings are being updated
        plex_settings = new_settings.get('Plex', {})
        
        plex_url_being_set = (plex_settings.get('url', '').strip() or 
                             file_management_settings.get('plex_url_for_symlink', '').strip())
        
        if plex_url_being_set:
            if media_server_type == 'plex':
                # User has selected Plex, clear Jellyfin/Emby settings
                logging.info("Plex URL detected and Media Server Type is Plex, clearing Jellyfin/Emby settings")
                
                # Clear Jellyfin/Emby settings in the new_settings
                if 'Debug' in new_settings:
                    new_settings['Debug']['emby_jellyfin_url'] = ''
                    new_settings['Debug']['emby_jellyfin_token'] = ''
                
                # Also clear these settings in the current config
                if 'Debug' not in config:
                    config['Debug'] = {}
                config['Debug']['emby_jellyfin_url'] = ''
                config['Debug']['emby_jellyfin_token'] = ''
            else:
                logging.info("Plex URL detected but Media Server Type is not Plex, keeping Jellyfin/Emby settings")

        # Function to recursively update the main config dictionary
        def update_nested_dict(current, new, parent_key=None):
            for key, value in new.items():
                # Special handling: preserve 'enabled' field in Content Sources (managed by Task Manager)
                if parent_key == 'Content Sources' and key == 'enabled':
                    # Skip updating the 'enabled' field - it's managed by Task Manager
                    continue

                if isinstance(value, dict) and key in current and isinstance(current[key], dict):
                     # Handle specific nested structures like 'Content Sources' or 'versions' if needed,
                     # otherwise, just recurse.
                     if key == 'Content Sources':
                         # Special handling for content sources if needed (e.g., merging vs replacing)
                         # Current behavior seems to merge/update existing and add new
                         for source_id, source_config in value.items():
                             if source_id in current[key]:
                                 # Preserve the enabled field when updating
                                 existing_enabled = current[key][source_id].get('enabled')
                                 # Recursively update existing source config
                                 update_nested_dict(current[key][source_id], source_config, parent_key='Content Sources')
                                 # Restore the enabled field if it existed
                                 if existing_enabled is not None and 'enabled' not in source_config:
                                     current[key][source_id]['enabled'] = existing_enabled
                             else:
                                 # Add new source config
                                 current[key][source_id] = source_config
                     elif key == 'versions' and 'Scraping' in current: # Check parent key for context
                         # Handle Scraping versions similarly
                         for version_id, version_config in value.items():
                             if version_id in current[key]:
                                 update_nested_dict(current[key][version_id], version_config)
                             else:
                                 current[key][version_id] = version_config
                     else:
                        update_nested_dict(current[key], value)
                else:
                    # Convert tags comma-string back to list when saving Content Sources
                    if parent_key == 'Content Sources' and key == 'tags' and isinstance(value, str):
                        value = [t.strip() for t in value.split(',') if t.strip()] if value.strip() else []
                    # Update or add the value
                    current[key] = value

        # Preserve collections dict — managed separately, not via form inputs
        _existing_colls = config.get('Plex Smart Collections', {}).get('collections', {})
        if not isinstance(_existing_colls, dict):
            _existing_colls = {}

        # Update the main config object with the new settings
        update_nested_dict(config, new_settings)

        # Clearing either Trakt credential disables the integration. Remove
        # OAuth state from both config.json and the legacy .pytrakt.json so a
        # stale refresh token cannot keep running after the fields are blank.
        from utilities.trakt_auth_cleanup import clear_stale_trakt_auth
        trakt_config_changed, trakt_legacy_changed = clear_stale_trakt_auth(config)
        if trakt_config_changed or trakt_legacy_changed:
            logging.info("Cleared stale Trakt OAuth state because Trakt credentials are incomplete")

        # Restore collections (prevent main save from overwriting with empty dict)
        if 'Plex Smart Collections' in config:
            if not isinstance(config['Plex Smart Collections'].get('collections'), dict) or not config['Plex Smart Collections'].get('collections'):
                config['Plex Smart Collections']['collections'] = _existing_colls

        # --- Simplified Staleness Update (if needed, relies on update_nested_dict) ---
        # If 'Staleness Threshold' exists in new_settings, update_nested_dict should handle it.
        # We might want explicit type conversion/validation *before* update_nested_dict though.
        if 'Staleness Threshold' in new_settings and 'staleness_threshold' in new_settings['Staleness Threshold']:
             try:
                 # Ensure the value in the main config dict is an int
                 config['Staleness Threshold']['staleness_threshold'] = int(new_settings['Staleness Threshold']['staleness_threshold'])
                 logging.info(f"Staleness threshold updated in main config object to: {config['Staleness Threshold']['staleness_threshold']}")
             except (ValueError, TypeError) as e:
                 logging.warning(f"Invalid value provided for staleness_threshold, cannot convert to int: {new_settings['Staleness Threshold']['staleness_threshold']}. Skipping update for this key. Error: {e}")
                 # Optionally remove the invalid key from the update or handle differently
                 # For now, update_nested_dict might have already placed the invalid value; save_config will likely fail if schema expects int.
                 # Or, revert the change in the config object if conversion fails:
                 # config['Staleness Threshold']['staleness_threshold'] = load_config().get('Staleness Threshold', {}).get('staleness_threshold', 7) # Revert to loaded or default


        # Handle Reverse Parser settings (Seems correctly handled by update_nested_dict)
        # Add validation if necessary

        # Save the updated main config object atomically (assuming save_config does this)
        save_config(config)
        logging.info("Main configuration saved successfully.")

        # Clear smart collection poster hash if those settings changed
        if 'Plex Smart Collections' in new_settings:
            try:
                from database.plex_smart_collections import _load_state, _save_state, _migrate_state
                _sc_state = _migrate_state(_load_state())
                _sc_state.setdefault('shared', {})['poster_hash'] = ''
                _save_state(_sc_state)
            except Exception:
                pass

        # Switching a preset collection on or off takes effect right away
        if 'Plex Preset Collections' in actually_changed_categories:
            try:
                from database.plex_preset_collections import run_preset_collections
                threading.Thread(target=run_preset_collections, daemon=True,
                                  name='plex-preset-collections').start()
            except Exception as e:
                logging.warning(f"Could not start preset collections sync: {e}")

        # For no changes or UI-only changes, skip cache clearing and reinitialization
        if skip_restart:
            logging.info("Skipping cache clearing and component reinitialization.")
            response_data = {
                "status": "success",
                "message": "Settings updated successfully. No restart required.",
                "requires_restart": False
            }
            return jsonify(response_data)

        # Clear content source cache files
        try:
            from routes.debug_routes import get_cache_files
            cache_data = get_cache_files()
            # get_cache_files now returns dict {'content_source': [(filename, label),...], 'other': [...]}
            cache_files = cache_data.get('content_source', []) if isinstance(cache_data, dict) else cache_data
            if cache_files:
                db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
                for filename, _ in cache_files:  # Unpack tuple (filename, display_label)
                    file_path = os.path.join(db_content_dir, filename)
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logging.info(f"Removed content source cache file: {file_path}")
                        except Exception as e:
                            logging.error(f"Failed to remove cache file {file_path}: {e}")
        except ImportError:
            logging.warning("Could not import get_cache_files from routes.debug_routes to clear cache.")
        except Exception as e:
            logging.error(f"An error occurred while clearing content source cache files: {e}")
        
        # Check if program was running before reinitialization
        was_program_running = False
        try:
            from routes.program_operation_routes import get_program_runner
            runner = get_program_runner()
            if runner and runner.is_running():
                was_program_running = True
                logging.info("Program was running before settings save. Will restart after reinitialization.")
        except Exception as e:
            logging.warning(f"Could not check program status before reinitialization: {e}")

        # Capture Flask app context for background thread
        app = current_app._get_current_object()

        # Define background reinitialization function
        def async_reinitialize():
            """Run component reinitialization in background thread"""
            with app.app_context():
                try:
                    logging.info("Starting background reinitialization of components...")

                    # Reinitialize components that depend on the config
                    from debrid import reset_provider
                    reset_provider()
                    logging.info("Debrid provider reset complete")

                    from usenet import reset_usenet_client
                    reset_usenet_client()
                    logging.info("Usenet provider client reset complete")

                    # Start, restart (config changed) or stop the optional
                    # built-in mount engine to match the new settings.
                    try:
                        from utilities import builtin_engine
                        from usenet.builtin_client import reset_builtin_client
                        reset_builtin_client()
                        logging.info(f"Built-in mount engine: {builtin_engine.sync()}")
                    except Exception as engine_err:
                        logging.warning(f"Built-in mount engine sync failed: {engine_err}")

                    from queues.queue_manager import QueueManager
                    QueueManager().reinitialize()
                    logging.info("QueueManager reinitialized")

                    # Restart program if it was running before
                    if was_program_running:
                        try:
                            # Stop the program first
                            from routes.program_operation_routes import stop_program, _execute_start_program, _program_op_lock
                            logging.info("Stopping program before restart...")
                            stop_result = stop_program()
                            if stop_result.get("status") != "success":
                                logging.warning(f"Stop returned non-success: {stop_result.get('message', 'Unknown')}")

                            # Wait for stop_program to fully release _program_op_lock before
                            # calling _execute_start_program. stop_program holds the lock during
                            # scheduler.shutdown(wait=True), which can block for many seconds if
                            # a Trakt job is mid-sleep. A fixed 2s sleep was too short; poll instead.
                            import time
                            _lock_wait = 0.0
                            _poll = 0.5
                            _max_lock_wait = 60.0
                            while _program_op_lock.locked() and _lock_wait < _max_lock_wait:
                                time.sleep(_poll)
                                _lock_wait += _poll
                            if _program_op_lock.locked():
                                logging.error(
                                    f"_program_op_lock still held after {_max_lock_wait:.0f}s — "
                                    "restart aborted; please start the program manually."
                                )
                                return
                            if _lock_wait > 1.0:
                                logging.info(f"Waited {_lock_wait:.1f}s for program_op_lock to free before restart")

                            # Start the program
                            logging.info("Starting program after settings change...")
                            start_result = _execute_start_program(skip_connectivity_check=True, is_restart=True)
                            if start_result.get("status") == "success":
                                logging.info("Program restarted successfully after settings save.")
                            else:
                                logging.warning(f"Failed to restart program after settings save: {start_result.get('message', 'Unknown error')}")
                        except Exception as restart_e:
                            logging.error(f"Error restarting program after settings save: {restart_e}", exc_info=True)
                    else:
                        # Program was not running - refresh content sources and update scheduled tasks
                        try:
                            from routes.program_operation_routes import get_program_runner
                            runner = get_program_runner()
                            if runner and runner.scheduler:
                                logging.info("Program not running - refreshing content sources and updating scheduled tasks...")

                                # Get currently scheduled content source tasks
                                existing_content_tasks = {
                                    job.id for job in runner.scheduler.get_jobs()
                                    if job.id.endswith('_wanted')
                                }

                                # Refresh content sources (updates enabled_tasks and task_intervals)
                                runner.get_content_sources(force_refresh=True)

                                # Get expected content source tasks from settings
                                expected_content_tasks = {
                                    task for task in runner.enabled_tasks
                                    if task.endswith('_wanted')
                                }

                                # Schedule new content source tasks
                                new_tasks = expected_content_tasks - existing_content_tasks
                                new_tasks_scheduled = 0
                                for task_name in new_tasks:
                                    interval = runner.task_intervals.get(task_name)
                                    if interval:
                                        if runner._schedule_task(task_name, interval, initial_run=True):
                                            new_tasks_scheduled += 1
                                            logging.info(f"Scheduled new content source task: {task_name}")

                                # Unschedule removed content source tasks
                                removed_tasks = existing_content_tasks - expected_content_tasks
                                tasks_unscheduled = 0
                                for task_name in removed_tasks:
                                    try:
                                        runner.scheduler.remove_job(task_name)
                                        tasks_unscheduled += 1
                                        logging.info(f"Unscheduled removed content source task: {task_name}")
                                    except Exception as e:
                                        logging.warning(f"Failed to unschedule task {task_name}: {e}")

                                logging.info(f"Content sources refreshed. {new_tasks_scheduled} new task(s) scheduled, {tasks_unscheduled} task(s) unscheduled.")
                        except Exception as refresh_e:
                            logging.error(f"Error refreshing content sources: {refresh_e}", exc_info=True)

                    logging.info("Background reinitialization completed successfully")

                except Exception as reinit_e:
                    logging.error(f"Error during background component reinitialization: {reinit_e}", exc_info=True)

        # Start reinitialization in background thread
        reinit_thread = threading.Thread(target=async_reinitialize, daemon=True, name="SettingsReinitThread")
        reinit_thread.start()
        logging.info("Settings saved successfully. Component reinitialization started in background.")

        response_data = {
            "status": "success",
            "message": "Settings updated successfully. Changes are being applied in the background.",
            "requires_restart": requires_restart
        }

        if requires_restart:
            response_data["restart_reasons"] = list(set(restart_reasons))

        return jsonify(response_data)
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}), 500

@settings_bp.route('/api/reverse_parser_settings', methods=['GET'])
def get_reverse_parser_settings():
    config = load_config()
    reverse_parser_settings = config.get('Reverse Parser', {})
    
    # Get all scraping versions
    all_scraping_versions = set(config.get('Scraping', {}).get('versions', {}).keys())
    
    # Get the current version order, or initialize it if it doesn't exist
    version_order = reverse_parser_settings.get('version_order', [])
    
    # Ensure version_terms exists
    version_terms = reverse_parser_settings.get('version_terms', {})
    
    # Create a new ordered version_terms dictionary
    ordered_version_terms = {}
    
    # First, add versions in the order specified by version_order
    for version in version_order:
        if version in all_scraping_versions:
            ordered_version_terms[version] = version_terms.get(version, [])
            all_scraping_versions.remove(version)
    
    # Then, add any remaining versions that weren't in version_order
    for version in all_scraping_versions:
        ordered_version_terms[version] = version_terms.get(version, [])
    
    # Update version_order to include any new versions
    version_order = list(ordered_version_terms.keys())
    
    # Update the settings
    reverse_parser_settings['version_terms'] = ordered_version_terms
    reverse_parser_settings['version_order'] = version_order
    
    # Ensure default_version is set and valid
    if 'default_version' not in reverse_parser_settings or reverse_parser_settings['default_version'] not in ordered_version_terms:
        reverse_parser_settings['default_version'] = next(iter(ordered_version_terms), None)
    
    return jsonify(reverse_parser_settings)

@settings_bp.route('/save_single_setting', methods=['POST'])
@admin_required
def save_single_setting():
    """Instant-save one schema-defined setting (used by the logo picker)."""
    from utilities.settings import set_setting
    section = request.form.get('section', '')
    key = request.form.get('key', '')
    value = request.form.get('value', '')

    schema = SETTINGS_SCHEMA.get(section, {}).get(key)
    if not isinstance(schema, dict):
        return jsonify({'status': 'error', 'error': f'Unknown setting {section}.{key}'}), 400

    setting_type = schema.get('type')
    if setting_type == 'boolean':
        value = value.lower() in ('true', '1', 'on', 'yes')
    elif setting_type == 'integer':
        try:
            value = int(value)
        except ValueError:
            return jsonify({'status': 'error', 'error': f'{key} must be an integer'}), 400
    elif setting_type != 'string':
        return jsonify({'status': 'error', 'error': f'{key} cannot be saved individually'}), 400

    choices = schema.get('choices')
    if choices and value not in choices:
        return jsonify({'status': 'error', 'error': f'Invalid value for {key}'}), 400

    set_setting(section, key, value)
    return jsonify({'status': 'success'})
