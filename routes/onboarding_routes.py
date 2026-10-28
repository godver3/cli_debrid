from flask import Blueprint, jsonify, request, render_template, redirect, url_for, flash, abort, session
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash
from utilities.settings import load_config, get_setting, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
from queues.config_manager import add_scraper, add_content_source, load_config
import logging
import os
import platform
from routes.trakt_routes import check_trakt_auth_status
import json
import requests
import urllib.parse
from .models import admin_required

onboarding_bp = Blueprint('onboarding', __name__)

USENET_BACKENDS = ('climount', 'nzbdav', 'zurg')
# New configs get this placeholder debrid key; it doesn't mean debrid is set up.
DEBRID_PLACEHOLDER_KEY = 'demo_key'

def _real_debrid_key(config):
    key = (config.get('Debrid Provider', {}).get('api_key') or '').strip()
    return '' if key == DEBRID_PLACEHOLDER_KEY else key

def _debrid_configured():
    return bool(get_setting('Debrid Provider', 'provider') and _real_debrid_key(load_config()))

def _usenet_configured():
    return bool(get_setting('Usenet Provider', 'enabled') and get_setting('Usenet Provider', 'url'))

def _media_provider_configured():
    """Return True if either a debrid provider or usenet provider is configured."""
    return _debrid_configured() or _usenet_configured()

def _metadata_configured():
    """Shows and movies are looked up on TVDB when a key is set, otherwise on Trakt."""
    return bool(get_setting('TVDB', 'api_key') or get_setting('Trakt', 'client_id'))

def _required_settings_complete():
    """True when everything step 2 asks for has been saved."""
    if not (_media_provider_configured() and _metadata_configured()):
        return False
    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        return bool(get_setting('File Management', 'symlinked_files_path')
                    and get_setting('File Management', 'original_files_path'))
    return bool(get_setting('Plex', 'url') and get_setting('Plex', 'token')
                and (get_setting('Plex', 'movie_libraries') or get_setting('Plex', 'shows_libraries')))

def _save_required_settings(form):
    """Save the step 2 form.

    Values are merged into the existing sections so keys the form doesn't
    show (set earlier in Settings, or by a previous run of onboarding) are kept.
    """
    config = load_config()
    file_management_type = form.get('file_collection_management', 'Plex')
    original_files_path = form.get('original_files_path', '').strip()
    is_symlink = file_management_type == 'Symlinked/Local'
    media_server = form.get('symlink_media_server', 'none') if is_symlink else 'plex'

    # Plex mode fills plex_url/plex_token; symlink mode fills the *_for_symlink fields.
    plex_url = (form.get('plex_url_for_symlink', '') if is_symlink else form.get('plex_url', '')).strip().rstrip('/')
    plex_token = (form.get('plex_token_for_symlink', '') if is_symlink else form.get('plex_token', '')).strip()

    file_management = config.setdefault('File Management', {})
    file_management.update({
        'file_collection_management': file_management_type,
        'original_files_path': original_files_path,
        'symlinked_files_path': form.get('symlinked_files_path', '/mnt/symlinked').strip(),
        # Filled in Plex mode too: Debrid Manager's Plex views read these in either mode.
        'plex_url_for_symlink': plex_url if media_server == 'plex' else '',
        'plex_token_for_symlink': plex_token if media_server == 'plex' else '',
    })
    if is_symlink:
        file_management['media_server_type'] = 'jellyfin' if media_server == 'jellyfin' else 'plex'
    file_management.setdefault('symlink_organize_by_type', True)

    def _libraries(name):
        return ','.join(lib.strip() for lib in form.get(name, '').split(',') if lib.strip())

    plex = config.setdefault('Plex', {})
    plex['mounted_file_location'] = original_files_path
    plex['update_plex_on_file_discovery'] = form.get('Plex.update_plex_on_file_discovery') == 'on'
    if media_server == 'plex' and plex_url and plex_token:
        plex['url'] = plex_url
        plex['token'] = plex_token
    if not is_symlink:
        plex['movie_libraries'] = _libraries('movie_libraries')
        plex['shows_libraries'] = _libraries('shows_libraries')

    if is_symlink:
        debug = config.setdefault('Debug', {})
        if media_server == 'jellyfin':
            debug['emby_jellyfin_url'] = form.get('emby_jellyfin_url', '').strip().rstrip('/')
            debug['emby_jellyfin_token'] = form.get('emby_jellyfin_token', '').strip()
        else:
            debug['emby_jellyfin_url'] = ''
            debug['emby_jellyfin_token'] = ''

    if 'use_debrid' in form or 'use_usenet' in form:
        use_debrid = form.get('use_debrid') == 'on'
        use_usenet = form.get('use_usenet') == 'on'
    else:
        use_usenet = form.get('provider_type') == 'usenet'
        use_debrid = not use_usenet

    debrid = config.setdefault('Debrid Provider', {})
    if use_debrid:
        debrid['provider'] = form.get('debrid_provider', 'RealDebrid')
        debrid['api_key'] = form.get('debrid_api_key', '').strip()
    elif debrid.get('api_key') == DEBRID_PLACEHOLDER_KEY:
        debrid['api_key'] = ''

    usenet = config.setdefault('Usenet Provider', {})
    usenet['enabled'] = use_usenet
    if use_usenet:
        backend = form.get('usenet_backend', 'climount').strip().lower()
        usenet['provider'] = backend if backend in USENET_BACKENDS else 'climount'
        usenet['url'] = form.get('usenet_url', '').strip().rstrip('/')
        usenet['api_token'] = form.get('usenet_api_token', '').strip()

    for section, field in (('TVDB', 'tvdb_api_key'), ('TMDB', 'tmdb_api_key')):
        if field in form:
            config.setdefault(section, {})['api_key'] = form.get(field, '').strip()

    # Trakt is optional — only save if the user provided credentials.
    trakt_client_id = form.get('trakt_client_id', '').strip()
    trakt_client_secret = form.get('trakt_client_secret', '').strip()
    if trakt_client_id or trakt_client_secret:
        trakt = config.setdefault('Trakt', {})
        trakt['client_id'] = trakt_client_id
        trakt['client_secret'] = trakt_client_secret

    save_config(config)

    if use_usenet:
        try:
            from usenet import reset_usenet_client
            reset_usenet_client()
        except Exception as e:
            logging.debug(f"Could not reset usenet client after onboarding save: {e}")

def get_next_onboarding_step():
    """The first onboarding step that still needs doing, so a returning user resumes there."""
    # Load the current configuration
    config = load_config()

    # Step 1: Check if the admin user is set up
    if current_user.is_default:
        return 1

    # Step 2: Check if required settings are configured
    if not _required_settings_complete():
        return 2

    # Step 3: Check if at least one scraper is configured
    if 'Scrapers' not in config or not config['Scrapers']:
        return 3
    
    # Step 3a: Check if versions are configured
    if 'Scraping' not in config or 'versions' not in config['Scraping'] or not config['Scraping']['versions']:
        return '3a'
    
    # Step 4: Check if at least one content source is configured
    if 'Content Sources' not in config or not config['Content Sources']:
        return 4
    
    # Step 5: Check if library management is configured
    if 'Libraries' not in config or not config['Libraries']:
        return 5

    # If all steps are completed, return the final step (6)
    return 6

@onboarding_bp.route('/')
@login_required
def onboarding():
    return render_template('onboarding.html', is_onboarding=True)

@onboarding_bp.route('/step/<step>', methods=['GET', 'POST'])
@login_required
@admin_required
def onboarding_step(step):
    from routes.auth_routes import db

    # Convert step to int if it's not '3a'
    try:
        step_num = int(step) if step != '3a' else step
    except ValueError:
        abort(404)

    # Validate step range
    if isinstance(step_num, int) and (step_num < 1 or step_num > 6):
        abort(404)
    elif step_num == '3a' and step != '3a':
        abort(404)
    
    config = load_config()
    can_proceed = False

    # Handle step 6 (final step)
    if step_num == 6:
        can_proceed = True  # Always allow finishing the onboarding process
        return render_template('onboarding_step_6.html', 
                             current_step=step_num, 
                             can_proceed=can_proceed, 
                             is_onboarding=True,
                             summary=_onboarding_summary(config))

    # Handle other steps...
    if step_num == 1:
        admin_created = not current_user.is_default
        can_proceed = admin_created

        if request.method == 'POST':
            new_username = request.form['new_username']
            new_password = request.form['new_password']
            confirm_password = request.form['confirm_password']
            if new_password == confirm_password:
                try:
                    current_user.username = new_username
                    current_user.password = generate_password_hash(new_password)
                    current_user.is_default = False
                    db.session.commit()
                    
                    # Re-login the user with the new credentials to refresh the session
                    from flask_login import login_user
                    login_user(current_user)
                    
                    return jsonify({'success': True})
                except Exception as e:
                    return jsonify({'success': False, 'error': str(e)})
            else:
                return jsonify({'success': False, 'error': 'Passwords do not match'})

        return render_template('onboarding_step_1.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               admin_created=admin_created, 
                               is_onboarding=True)
       
    elif step_num == 2:
        is_windows = platform.system() == 'Windows'

        if request.method == 'POST':
            try:
                _save_required_settings(request.form)
                return jsonify({'success': True, 'can_proceed': _required_settings_complete()})
            except Exception as e:
                logging.error(f"Error saving onboarding settings: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)})

        config = load_config()
        can_proceed = _required_settings_complete()
        trakt_status = json.loads(check_trakt_auth_status().get_data(as_text=True))
        allow_windows_symlinks_value = config.get('Debug', {}).get('use_symlinks_on_windows', False)
        usenet = config.get('Usenet Provider', {})
        usenet_backend = (usenet.get('provider') or 'climount').strip().lower()

        return render_template('onboarding_step_2.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               trakt_status=trakt_status,
                               is_onboarding=True,
                               is_windows=is_windows,
                               allow_windows_symlinks=allow_windows_symlinks_value,
                               use_debrid=bool(_real_debrid_key(config)),
                               debrid_api_key=_real_debrid_key(config),
                               use_usenet=bool(usenet.get('enabled')),
                               usenet_backend=usenet_backend if usenet_backend in USENET_BACKENDS else 'climount')

    elif step_num == 3:
        config = load_config()
        # Debrid users get Torrentio (no account or URL needed) so they can just
        # click Next. Only once per session, so deleting it on purpose sticks.
        added_recommended = False
        if (not config.get('Scrapers') and _debrid_configured()
                and not session.get('onboarding_recommended_scraper_added')):
            add_scraper('Torrentio', {'enabled': True})
            session['onboarding_recommended_scraper_added'] = True
            added_recommended = True
            config = load_config()
        can_proceed = 'Scrapers' in config and bool(config['Scrapers'])
        return render_template('onboarding_step_3.html',
                               current_step=step_num,
                               can_proceed=can_proceed,
                               settings=config,
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True,
                               added_recommended_scraper=added_recommended,
                               use_debrid=_debrid_configured(),
                               use_usenet=_usenet_configured())

    elif step_num == '3a':
        config = load_config()
        # Start everyone on the 1080p Default version so Next works right away.
        added_default = False
        if (not config.get('Scraping', {}).get('versions')
                and not session.get('onboarding_default_version_added')):
            from routes.settings_routes import build_default_version
            config.setdefault('Scraping', {})['versions'] = {'Default': build_default_version()}
            save_config(config)
            session['onboarding_default_version_added'] = True
            added_default = True
        can_proceed = 'Scraping' in config and 'versions' in config['Scraping'] and bool(config['Scraping']['versions'])
        return render_template('onboarding_step_3a.html',
                               current_step=step_num,
                               can_proceed=can_proceed,
                               settings=config,
                               is_onboarding=True,
                               added_default_version=added_default)

    elif step_num == 4:
        config = load_config()
        can_proceed = 'Content Sources' in config and bool(config['Content Sources'])
        # Get the dictionary of version configurations
        versions_dict = config.get('Scraping', {}).get('versions', {})
        # Extract the names (keys) into a list
        configured_versions = list(versions_dict.keys())
        return render_template('onboarding_step_4.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, 
                               configured_versions=configured_versions,
                               is_onboarding=True)

    elif step_num == 5:
        # Library Management step
        config = load_config()
        can_proceed = 'Libraries' in config and bool(config['Libraries'])
        return render_template('onboarding_step_5.html', 
                             current_step=step_num, 
                             can_proceed=can_proceed,
                             settings=config, 
                             is_onboarding=True)

    # If no matching step is found
    abort(404)

@onboarding_bp.route('/complete', methods=['POST'])
@login_required
def complete_onboarding():
    from routes.auth_routes import db

    try:
        current_user.onboarding_complete = True
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    
@onboarding_bp.route('/update_can_proceed', methods=['POST'])
@login_required
def update_can_proceed():
    data = request.json
    step = data.get('step')
    can_proceed = data.get('can_proceed')
    
    # Convert step to string to match the check list
    step_str = str(step)
    
    if step_str in ['1', '2', '3', '3a', '4', '5']:
        session[f'onboarding_step_{step_str}_can_proceed'] = can_proceed
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid step'}), 400
    


@onboarding_bp.route('/setup_admin', methods=['GET', 'POST'])
@login_required
def setup_admin():
    from routes.auth_routes import db
    from routes.auth_routes import User

    if not current_user.is_default:
        return redirect(url_for('onboarding.onboarding_step', step=1))
    if request.method == 'POST':
        new_username = request.form['new_username']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        if new_password != confirm_password:
            flash('Passwords do not match.', 'error')
        else:
            existing_user = User.query.filter_by(username=new_username).first()
            if existing_user and existing_user.id != current_user.id:
                flash('Username already exists.', 'error')
            else:
                try:
                    # Delete all default admin accounts
                    User.query.filter_by(is_default=True).delete()
                    
                    # Create the new admin account
                    new_admin = User(username=new_username, 
                                     password=generate_password_hash(new_password),
                                     role='admin',
                                     is_default=False,
                                     onboarding_complete=False)  # Set onboarding_complete to False
                    db.session.add(new_admin)
                    db.session.commit()
                    
                    # Log out the current user (original admin) and log in the new admin
                    logout_user()
                    login_user(new_admin)
                    
                    # Redirect to the first onboarding step
                    return redirect(url_for('onboarding.onboarding_step', step=1))
                except Exception as e:
                    db.session.rollback()
                    flash(f'An error occurred: {str(e)}', 'error')
    return render_template('setup_admin.html', is_onboarding=True)

@onboarding_bp.route('/content_sources/add', methods=['POST'])
@admin_required
def add_onboarding_content_source():
    data = request.json
    source_type = data.get('type')
    source_config = data.get('config')
    
    if not source_type or not source_config:
        return jsonify({'success': False, 'error': 'Invalid content source data'}), 400

    # --- Convert versions list to dictionary format --- START
    # if 'versions' in source_config:
    #     if isinstance(source_config['versions'], list):
    #         source_config['versions'] = {v: True for v in source_config['versions']}
    #     elif not isinstance(source_config['versions'], dict):
    #         # Import logging if not already imported at the top
    #         import logging 
    #         logging.warning(f"Onboarding: Invalid 'versions' format for {source_type}: {source_config['versions']}. Resetting to empty dict.")
    #         source_config['versions'] = {}
    # --- Convert versions list to dictionary format --- END

    try:
        # Ensure 'versions' is present and is a list if it exists in source_config
        if 'versions' in source_config and not isinstance(source_config['versions'], list):
             # If it's not a list (e.g., the frontend somehow sent it wrong),
             # default to an empty list or handle error as appropriate.
             # For now, let's ensure it's at least an empty list if present but not list.
             logging.warning(f"Onboarding: Received non-list 'versions' for {source_type}. Ensuring list format.")
             source_config['versions'] = list(source_config.get('versions', [])) # Attempt conversion or default

        new_source_id = add_content_source(source_type, source_config)

        # The runner builds its task list once, so without this the new source
        # never gets a scheduled task until the app restarts.
        from routes.settings_routes import _reload_components_after_settings_change
        _reload_components_after_settings_change('content_source')

        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        # Log the error including traceback for better debugging
        import traceback
        logging.error(f"Error in add_onboarding_content_source: {str(e)}\\n{traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@onboarding_bp.route('/content_sources/get', methods=['GET'])
@admin_required  # returns every content source's config, including Other Plex Watchlist account tokens
def get_onboarding_content_sources():
    config = load_config()
    content_source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    content_sources = config.get('Content Sources', {})
    logging.debug(f"Retrieved content sources: {content_sources}")
    return jsonify({
        'content_sources': content_sources,
        'source_types': content_source_types,
        'settings': SETTINGS_SCHEMA['Content Sources']['schema']
    })

@onboarding_bp.route('/scrapers/add', methods=['POST'])
@admin_required
def add_onboarding_scraper():
    logging.info(f"Received request to add scraper during onboarding. Content-Type: {request.content_type}")
    logging.info(f"Request data: {request.data}")
    try:
        if request.is_json:
            data = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        logging.info(f"Parsed data: {data}")
        
        scraper_type = data.get('type')
        scraper_config = data.get('config')
        
        if not scraper_type:
            return jsonify({'success': False, 'error': 'No scraper type provided'}), 400
        
        if not scraper_config:
            return jsonify({'success': False, 'error': 'No scraper config provided'}), 400

        # Use the add_scraper function from config_manager
        new_scraper_id = add_scraper(scraper_type, scraper_config)
        
        # Log the updated config after adding the scraper
        updated_config = load_config()
        logging.info(f"Updated config after adding scraper: {updated_config}")
        
        return jsonify({'success': True, 'scraper_id': new_scraper_id})
    except Exception as e:
        logging.error(f"Error adding scraper during onboarding: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@onboarding_bp.route('/scrapers/get', methods=['GET'])
@admin_required  # returns every scraper's config, including indexer API keys
def get_onboarding_scrapers():
    config = load_config()
    scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())
    return jsonify({
        'scrapers': config.get('Scrapers', {}),
        'scraper_types': scraper_types
    })

def _coerce_to_schema(values, schema, skip=('type',)):
    """Turn popup form values into the types the schema expects.

    Checkboxes arrive as 'on' or are left out entirely, numbers arrive as strings.
    Keys the schema doesn't know are dropped.
    """
    result = {}
    for key, field in schema.items():
        if key in skip:
            continue
        field_type = field.get('type')
        if field_type == 'boolean':
            result[key] = values.get(key) in (True, 'on', 'true', '1')
        elif key not in values:
            continue
        elif field_type == 'integer':
            try:
                result[key] = int(values[key])
            except (TypeError, ValueError):
                continue
        elif field_type == 'float':
            try:
                result[key] = float(values[key])
            except (TypeError, ValueError):
                continue
        else:
            result[key] = values[key]
    return result

@onboarding_bp.route('/scrapers/update', methods=['POST'])
@admin_required
def update_onboarding_scraper():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get('scraper_id')
    config = load_config()
    scraper = config.get('Scrapers', {}).get(scraper_id)
    if not scraper:
        return jsonify({'success': False, 'error': 'Scraper not found'}), 404
    schema = SETTINGS_SCHEMA['Scrapers']['schema'].get(scraper.get('type'), {})
    # 'enabled' isn't in the edit form, so keep whatever it was.
    scraper.update(_coerce_to_schema(data.get('config') or {}, schema, skip=('type', 'enabled')))
    save_config(config)
    return jsonify({'success': True})

@onboarding_bp.route('/content_sources/update', methods=['POST'])
@admin_required
def update_onboarding_content_source():
    data = request.get_json(silent=True) or {}
    source_id = data.get('source_id')
    values = data.get('config') or {}
    config = load_config()
    source = config.get('Content Sources', {}).get(source_id)
    if not source:
        return jsonify({'success': False, 'error': 'Content source not found'}), 404
    schema = SETTINGS_SCHEMA['Content Sources']['schema'].get(source.get('type'), {})
    source.update(_coerce_to_schema(values, schema, skip=('type', 'enabled', 'versions')))
    if 'display_name' in values:
        source['display_name'] = values['display_name']
    if isinstance(values.get('versions'), list):
        source['versions'] = values['versions']
    save_config(config)
    return jsonify({'success': True})

@onboarding_bp.route('/test/<what>', methods=['POST'])
@admin_required
def onboarding_test(what):
    """The step 2 "Test" buttons: check one thing without saving anything."""
    from routes.settings_validation_routes import (
        validate_usenet_provider, validate_tvdb_api_key, validate_tmdb_api_key, check_mount_path)
    data = request.get_json(silent=True) or {}
    warning = False
    if what == 'usenet':
        ok, message = validate_usenet_provider(data.get('backend'), data.get('url'), data.get('token', ''))
    elif what == 'tvdb':
        ok, message = validate_tvdb_api_key(data.get('api_key'))
    elif what == 'tmdb':
        ok, message = validate_tmdb_api_key(data.get('api_key'))
    elif what == 'mount':
        ok, message, warning = check_mount_path(data.get('path'))
    else:
        abort(404)
    return jsonify({'success': ok, 'message': message, 'warning': warning})

def _onboarding_summary(config):
    """What onboarding set up, in plain words, for the final step."""
    file_management = config.get('File Management', {})
    mode = file_management.get('file_collection_management', 'Plex')
    usenet = config.get('Usenet Provider', {})
    debrid = config.get('Debrid Provider', {})
    providers = []
    if _real_debrid_key(config):
        providers.append(debrid.get('provider', 'RealDebrid'))
    if usenet.get('enabled') and usenet.get('url'):
        backend = (usenet.get('provider') or 'climount').lower()
        providers.append({'climount': 'cli_mount', 'nzbdav': 'NzbDAV', 'zurg': 'Zurg'}.get(backend, backend)
                         + ' (usenet)')
    if mode == 'Symlinked/Local':
        if config.get('Debug', {}).get('emby_jellyfin_url'):
            media_server = 'Jellyfin / Emby'
        elif file_management.get('plex_url_for_symlink'):
            media_server = 'Plex'
        else:
            media_server = 'None'
    else:
        media_server = 'Plex'
    if config.get('TVDB', {}).get('api_key'):
        metadata = 'TVDB' + (' + TMDB' if config.get('TMDB', {}).get('api_key') else '')
    elif config.get('Trakt', {}).get('client_id'):
        metadata = 'Trakt'
    else:
        metadata = 'Not set'
    rows = [
        ('Library mode', 'Symlinks' if mode == 'Symlinked/Local' else 'Plex (direct mount)'),
        ('Downloads from', ', '.join(providers) or 'Not set'),
        ('Media server', media_server),
        ('Metadata', metadata),
        ('Mount path', file_management.get('original_files_path') or 'Not set'),
    ]
    if mode == 'Symlinked/Local':
        rows.append(('Symlink folder', file_management.get('symlinked_files_path') or 'Not set'))
    else:
        libraries = ', '.join(filter(None, [config.get('Plex', {}).get('movie_libraries'),
                                            config.get('Plex', {}).get('shows_libraries')]))
        rows.append(('Plex libraries', libraries or 'Not set'))
    rows += [
        ('Scrapers', ', '.join(config.get('Scrapers', {}).keys()) or 'None'),
        ('Versions', ', '.join(config.get('Scraping', {}).get('versions', {}).keys()) or 'None'),
        ('Content sources', ', '.join(
            s.get('display_name') or sid for sid, s in config.get('Content Sources', {}).items()) or 'None'),
    ]
    return rows

def _scraper_reachable(scraper):
    """Quick check that a scraper's server answers. None when there's nothing to check."""
    scraper_type = scraper.get('type')
    url = scraper.get('url') or scraper.get('base_url')
    if scraper_type == 'Torrentio':
        url = 'https://torrentio.strem.fun/manifest.json'
    elif scraper_type in ('Nyaa', 'OldNyaa'):
        url = 'https://nyaa.si'
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException as e:
        return False, f"Could not reach {url}: {e}"
    if r.status_code >= 500:
        return False, f"{url} returned HTTP {r.status_code}"
    return True, f"Reachable ({url})"

@onboarding_bp.route('/health', methods=['GET'])
@admin_required
def onboarding_health():
    """Test everything onboarding set up, for the final step."""
    from routes.settings_validation_routes import (
        validate_plex_settings, validate_debrid_api_key, validate_usenet_provider,
        validate_tvdb_api_key, validate_tmdb_api_key, check_mount_path, validate_path_exists)
    config = load_config()
    checks = []

    def add(name, ok, message, warning=False):
        checks.append({'name': name, 'valid': ok, 'message': message, 'warning': warning})

    debrid = config.get('Debrid Provider', {})
    usenet = config.get('Usenet Provider', {})
    if _real_debrid_key(config):
        add(f"Debrid ({debrid.get('provider', 'RealDebrid')})",
            *validate_debrid_api_key(_real_debrid_key(config), debrid.get('provider', 'RealDebrid')))
    if usenet.get('enabled') and usenet.get('url'):
        add('Usenet provider', *validate_usenet_provider(usenet.get('provider'), usenet['url'],
                                                         usenet.get('api_token', '')))
    if not checks:
        add('Provider', False, 'No debrid or usenet provider is set up (step 2)')

    file_management = config.get('File Management', {})
    mount_path = file_management.get('original_files_path')
    if mount_path:
        ok, message, warning = check_mount_path(mount_path)
        add('Mount', ok, message, warning)
    else:
        add('Mount', False, 'No mount path set. cli_debrid can\'t see your files without one (step 2)', True)

    if file_management.get('file_collection_management') == 'Symlinked/Local':
        symlink_path = file_management.get('symlinked_files_path', '')
        if os.path.isdir(symlink_path):
            add('Symlink folder', *validate_path_exists(symlink_path))
        else:
            parent_ok, parent_msg = validate_path_exists(os.path.dirname(symlink_path.rstrip('/')) or '/')
            add('Symlink folder', parent_ok,
                'Will be created on first start' if parent_ok else f'Parent folder: {parent_msg}')
        jellyfin_url = config.get('Debug', {}).get('emby_jellyfin_url')
        plex_url = file_management.get('plex_url_for_symlink')
        if plex_url:
            add('Plex', *validate_plex_settings(plex_url, file_management.get('plex_token_for_symlink', '')))
        elif jellyfin_url:
            from utilities.jellyfin_auth import jellyfin_auth_headers
            try:
                r = requests.get(f"{jellyfin_url.rstrip('/')}/System/Info", timeout=5,
                                 headers=jellyfin_auth_headers(config['Debug'].get('emby_jellyfin_token', '')))
                add('Jellyfin / Emby', r.status_code == 200,
                    'Connected' if r.status_code == 200 else f'Returned HTTP {r.status_code}')
            except requests.RequestException as e:
                add('Jellyfin / Emby', False, f'Could not connect: {e}')
        else:
            add('Media server', True, 'None set, so new files won\'t trigger a library scan', True)
    else:
        plex = config.get('Plex', {})
        add('Plex', *validate_plex_settings(plex.get('url', ''), plex.get('token', '')))

    tvdb_key = config.get('TVDB', {}).get('api_key')
    tmdb_key = config.get('TMDB', {}).get('api_key')
    if tvdb_key:
        add('TVDB', *validate_tvdb_api_key(tvdb_key))
    elif config.get('Trakt', {}).get('client_id'):
        add('Metadata', True, 'Using Trakt (a TVDB key is recommended instead)', True)
    else:
        add('Metadata', False, 'No TVDB key or Trakt credentials (step 2)')
    if tmdb_key:
        add('TMDB', *validate_tmdb_api_key(tmdb_key))
    else:
        add('TMDB', False, 'Not set. Posters, release dates and Discover need it', True)

    scrapers = config.get('Scrapers', {})
    if not scrapers:
        add('Scrapers', False, 'No scrapers (step 3)')
    for scraper_id, scraper in scrapers.items():
        result = _scraper_reachable(scraper)
        if result:
            add(f'Scraper: {scraper_id}', *result)
        else:
            add(f'Scraper: {scraper_id}', True, 'Added')
    if not config.get('Scraping', {}).get('versions'):
        add('Versions', False, 'No versions (step 3a)')
    if not config.get('Content Sources'):
        add('Content sources', False, 'No content sources, so nothing will be requested yet (step 4)', True)

    blocking = [c for c in checks if not c['valid'] and not c['warning']]
    return jsonify({'ok': not blocking, 'checks': checks})

@onboarding_bp.route('/settings/api/update', methods=['POST'])
@admin_required
def update_settings():
    try:
        data = request.json
        config = load_config()
        
        # Update each setting in the config
        for setting_path, value in data.items():
            # Split the path into category and key
            category, key = setting_path.split('.')
            
            # Ensure category exists in config
            if category not in config:
                config[category] = {}
            
            # Update the setting
            config[category][key] = value
        
        # Save the updated config
        save_config(config)
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@onboarding_bp.route('/settings/validate/onboarding-settings', methods=['POST'])
@admin_required
def validate_onboarding_settings():
    try:
        data = request.json
        management_type = data.get('management_type')
        config = load_config()
        
        # For skip option
        if management_type == 'skip':
            # Save minimal library configuration
            if 'Libraries' not in config:
                config['Libraries'] = {}
            config['Libraries']['setup_skipped'] = True
            save_config(config)
            return jsonify({
                'valid': True,
                'checks': [{
                    'name': 'Library Management',
                    'valid': True,
                    'message': 'Library management setup has been skipped.'
                }]
            })
            
        # For fresh Plex setup
        elif management_type == 'fresh':
            checks = []
            is_valid = True
            
            # Check if Plex settings are configured
            plex_settings = config.get('Plex', {})
            if not plex_settings.get('url') or not plex_settings.get('token'):
                is_valid = False
                checks.append({
                    'name': 'Plex Configuration',
                    'valid': False,
                    'message': 'Plex URL and token are required for fresh setup.'
                })
            else:
                checks.append({
                    'name': 'Plex Configuration',
                    'valid': True,
                    'message': 'Plex settings are properly configured.'
                })
                
            return jsonify({
                'valid': is_valid,
                'checks': checks
            })
            
        # For existing Plex setups
        elif management_type in ['plex_direct', 'plex_symlink', 'Plex', 'Symlinked/Local']:
            checks = []
            is_valid = True

            # Validate based on setup type
            if management_type in ('plex_direct', 'Plex'):
                # Check for direct mount requirements - only validate if path is provided
                mount_path = config.get('File Management', {}).get('original_files_path')
                # Since original_files_path is optional, we don't need to validate it if it's empty
                if mount_path:
                    checks.append({
                        'name': 'Mount Configuration',
                        'valid': True,
                        'message': 'Mount path is configured. Note: Path existence cannot be verified within Docker.'
                    })
                # Don't add any validation check if mount_path is empty since it's optional
                    
            elif management_type in ('plex_symlink', 'Symlinked/Local'):
                # Check for symlink requirements
                symlink_path = config.get('File Management', {}).get('symlinked_files_path')
                if not symlink_path:
                    is_valid = False
                    checks.append({
                        'name': 'Symlink Configuration',
                        'valid': False,
                        'message': 'Symlink path is not configured.'
                    })
                else:
                    checks.append({
                        'name': 'Symlink Configuration',
                        'valid': True,
                        'message': 'Symlink path is configured. Note: Path existence cannot be verified within Docker.'
                    })
                    
                # Check original files path only if provided (optional)
                original_path = config.get('File Management', {}).get('original_files_path')
                if original_path:
                    checks.append({
                        'name': 'Original Files Path',
                        'valid': True,
                        'message': 'Original files path is configured. Note: Path existence cannot be verified within Docker.'
                    })
                    
            return jsonify({
                'valid': is_valid,
                'checks': checks
            })
            
        else:
            return jsonify({
                'valid': False,
                'checks': [{
                    'name': 'Configuration Error',
                    'valid': False,
                    'message': f'Invalid management type: {management_type}'
                }]
            }), 400
            
    except Exception as e:
        return jsonify({
            'valid': False,
            'checks': [{
                'name': 'System Error',
                'valid': False,
                'message': str(e)
            }]
        }), 500

@onboarding_bp.route('/plex/discover', methods=['GET'])
@admin_required
def discover_plex_servers():
    """Discover available Plex servers using X-Plex-Token."""
    try:
        from plexapi.myplex import MyPlexAccount
        from plexapi.exceptions import Unauthorized
        
        token = request.args.get('token')
        if not token:
            return jsonify({
                'success': False,
                'error': 'No token provided'
            }), 400
            
        try:
            account = MyPlexAccount(token=token)
            resources = account.resources()
            servers = []
            
            for resource in resources:
                if resource.provides == 'server':
                    connections = []
                    for connection in resource.connections:
                        if connection.local:
                            connections.append({
                                'uri': connection.uri,
                                'local': True
                            })
                        else:
                            connections.append({
                                'uri': connection.uri,
                                'local': False
                            })
                    
                    servers.append({
                        'name': resource.name,
                        'clientIdentifier': resource.clientIdentifier,
                        'connections': connections,
                        'owned': resource.owned,
                        'home': resource.home
                    })
            
            return jsonify({
                'success': True,
                'servers': servers,
                'username': account.username
            })
            
        except Unauthorized:
            return jsonify({
                'success': False,
                'error': 'Invalid Plex token'
            }), 401
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@onboarding_bp.route('/plex/libraries', methods=['GET'])
@admin_required
def get_plex_libraries():
    """Get available libraries from a Plex server."""
    try:
        from plexapi.server import PlexServer
        from plexapi.exceptions import Unauthorized, NotFound
        
        url = request.args.get('url')
        token = request.args.get('token')
        
        if not url or not token:
            return jsonify({
                'success': False,
                'error': 'URL and token are required'
            }), 400
            
        try:
            # Clean up URL
            url = url.rstrip('/')
            plex = PlexServer(url, token)
            sections = plex.library.sections()
            
            libraries = []
            for section in sections:
                libraries.append({
                    'key': section.key,
                    'title': section.title,
                    'type': section.type,
                    'locations': section.locations
                })
            
            return jsonify({
                'success': True,
                'libraries': libraries
            })
            
        except Unauthorized:
            return jsonify({
                'success': False,
                'error': 'Invalid Plex token'
            }), 401
        except NotFound:
            return jsonify({
                'success': False,
                'error': 'Could not connect to Plex server'
            }), 404
        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@onboarding_bp.route('/plex/auth/pin', methods=['POST'])
@admin_required
def create_plex_pin():
    try:
        import uuid
        
        # Get the client ID from the session or generate a new one
        client_id = session.get('plex_client_id', str(uuid.uuid4()))
        session['plex_client_id'] = client_id  # Store for future use
        
        # Define headers for Plex API
        headers = {
            'Accept': 'application/json',
            'X-Plex-Product': 'cli_debrid',
            'X-Plex-Version': '0.6.06',
            'X-Plex-Client-Identifier': client_id,
            'X-Plex-Platform': 'Web',
            'X-Plex-Platform-Version': '1.0',
            'X-Plex-Device': 'Browser',
            'X-Plex-Device-Name': 'cli_debrid_browser',
            'X-Plex-Device-Screen-Resolution': '1920x1080',
            'X-Plex-Language': 'en',
            'X-Plex-Model': 'hosted'
        }
        
        # Make request to Plex to create a pin
        response = requests.post(
            'https://plex.tv/api/v2/pins',
            headers=headers,
            json={'strong': True}
        )
        
        if response.status_code == 201:
            pin_data = response.json()
            
            # Store pin data in session
            session['plex_pin_id'] = pin_data['id']
            
            # Construct the auth URL - using the correct format
            auth_url = (
                'https://app.plex.tv/auth#?' + 
                f'clientID={client_id}&' +
                f'code={pin_data["code"]}&' +
                'context%5Bdevice%5D%5Bproduct%5D=cli_debrid&' +
                'context%5Bdevice%5D%5Bversion%5D=0.6.06&' +
                'context%5Bdevice%5D%5Bplatform%5D=Web&' +
                'context%5Bdevice%5D%5BplatformVersion%5D=1.0&' +
                'context%5Bdevice%5D%5Bdevice%5D=Browser&' +
                'context%5Bdevice%5D%5BdeviceName%5D=cli_debrid_browser&' +
                'context%5Bdevice%5D%5Bmodel%5D=hosted&' +
                'context%5Bdevice%5D%5BscreenResolution%5D=1920x1080&' +
                'context%5Bdevice%5D%5Blanguage%5D=en&' +
                f'forwardUrl={urllib.parse.quote(url_for("onboarding.plex_auth_callback", pinId=pin_data["id"], clientId=client_id, _external=True))}'
            )
            
            return jsonify({
                'success': True,
                'pin': {
                    'id': pin_data['id'],
                    'code': pin_data['code'],
                    'clientId': client_id,
                    'authUrl': auth_url
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to create pin: {response.status_code}'
            })
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@onboarding_bp.route('/plex/auth/callback')
def plex_auth_callback():
    """Handle Plex OAuth callback."""
    try:
        pin_id = request.args.get('pinId')
        client_id = request.args.get('clientId')
        
        if not pin_id or not client_id:
            return render_template('plex_auth_callback.html', 
                                 success=False, 
                                 error='Missing required parameters')
        
        return render_template('plex_auth_callback.html')
            
    except Exception as e:
        return render_template('plex_auth_callback.html', 
                             success=False, 
                             error=str(e))

@onboarding_bp.route('/plex/verify_token', methods=['POST'])
@admin_required
def verify_plex_token():
    """Verify if a Plex token is valid."""
    try:
        token = request.json.get('token')
        if not token:
            return jsonify({
                'success': False,
                'error': 'Token is required'
            }), 400
            
        # Get client ID from session or request
        client_id = session.get('plex_client_id') or request.json.get('clientId')
        if not client_id:
            return jsonify({
                'success': False,
                'error': 'No client ID found'
            }), 400
            
        # Make request to verify token
        response = requests.get(
            'https://plex.tv/api/v2/user',
            headers={
                'Accept': 'application/json',
                'X-Plex-Token': token,
                'X-Plex-Client-Identifier': client_id,
                'X-Plex-Product': 'cli_debrid'
            }
        )
        
        if response.status_code == 200:
            user_data = response.json()
            return jsonify({
                'success': True,
                'valid': True,
                'username': user_data.get('username', 'Unknown')
            })
        elif response.status_code == 401:
            return jsonify({
                'success': True,
                'valid': False,
                'error': 'Invalid token'
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to verify token: {response.status_code}'
            })
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@onboarding_bp.route('/plex/auth/pin/check', methods=['POST'])
@admin_required
def check_plex_pin():
    """Check the status of a Plex auth pin."""
    try:
        pin_id = request.json.get('pin_id')
        if not pin_id:
            return jsonify({
                'success': False,
                'error': 'Pin ID is required'
            }), 400

        # Get the client ID from session
        client_id = session.get('plex_client_id')
        if not client_id:
            return jsonify({
                'success': False,
                'error': 'No client ID found in session'
            }), 400

        # Make request to check pin status
        headers = {
            'Accept': 'application/json',
            'X-Plex-Client-Identifier': client_id
        }

        response = requests.get(
            f'https://plex.tv/api/v2/pins/{pin_id}',
            headers=headers
        )

        if response.status_code == 200:
            pin_data = response.json()
            if pin_data.get('authToken'):
                # Pin has been authorized
                return jsonify({
                    'success': True,
                    'token': pin_data['authToken']
                })
            else:
                # Pin is still waiting for authorization
                return jsonify({
                    'success': False,
                    'error': 'waiting_for_auth'
                })
        elif response.status_code == 404:
            return jsonify({
                'success': False,
                'error': 'Pin not found'
            }), 404
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to check pin status: {response.status_code}'
            }), response.status_code

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@onboarding_bp.route('/skip', methods=['GET', 'POST'])
@login_required
def skip_onboarding():
    from routes.auth_routes import db

    if request.method == 'GET':
        return render_template('skip_onboarding.html', is_onboarding=True)

    try:
        current_user.onboarding_complete = True
        db.session.commit()
        return jsonify({'success': True, 'message': 'Onboarding skipped successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
