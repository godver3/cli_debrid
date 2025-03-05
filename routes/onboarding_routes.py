from flask import Blueprint, jsonify, request, render_template, redirect, url_for, flash, abort, session
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash
from settings import load_config, get_setting, save_config
from settings_schema import SETTINGS_SCHEMA
from config_manager import add_scraper, add_content_source, load_config
import logging
import platform
from routes.trakt_routes import check_trakt_auth_status
import json

onboarding_bp = Blueprint('onboarding', __name__)

def get_next_onboarding_step():
    # Load the current configuration
    config = load_config()
    
    # Step 1: Check if the admin user is set up
    if current_user.is_default:
        return 1
    
    # Step 2: Check if required settings are configured
    required_settings = [
        ('Plex', 'url'),
        ('Plex', 'token'),
        ('Plex', 'shows_libraries'),
        ('Plex', 'movie_libraries'),
        ('Plex', 'update_plex_on_file_discovery'),
        ('Plex', 'mounted_file_location'),
        ('Debrid Provider', 'provider'),
        ('Debrid Provider', 'api_key'),
        ('Trakt', 'client_id'),
        ('Trakt', 'client_secret')
    ]
    
    for category, key in required_settings:
        if not get_setting(category, key):
            return 2
    
    # Check if Trakt is authorized
    trakt_status = json.loads(check_trakt_auth_status().get_data(as_text=True))
    if trakt_status['status'] != 'authorized':
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
                             is_onboarding=True)

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
        required_settings = [
            ('Debrid Provider', 'provider'),
            ('Debrid Provider', 'api_key'),
            ('Trakt', 'client_id'),
            ('Trakt', 'client_secret')
        ]

        # Check if platform is Windows
        is_windows = platform.system() == 'Windows'

        if request.method == 'POST':
            try:
                config = load_config()
                
                # Handle file management type
                file_management_type = request.form.get('file_collection_management', 'Plex')
                original_files_path = request.form.get('original_files_path', '/mnt/zurg/__all__')
                
                # Get Plex URL and token - try both regular and symlink fields
                plex_url = request.form.get('plex_url_for_symlink', '') or request.form.get('plex_url', '')
                plex_token = request.form.get('plex_token_for_symlink', '') or request.form.get('plex_token', '')
                
                # Debug logging
                logging.info("Form data received:")
                logging.info(f"file_management_type: {file_management_type}")
                logging.info(f"plex_url from regular field: {request.form.get('plex_url', '')}")
                logging.info(f"plex_url from symlink field: {request.form.get('plex_url_for_symlink', '')}")
                logging.info(f"plex_token from regular field: {request.form.get('plex_token', '')}")
                logging.info(f"plex_token from symlink field: {request.form.get('plex_token_for_symlink', '')}")
                logging.info(f"Final plex_url: {plex_url}")
                logging.info(f"Final plex_token: {plex_token}")
                
                # Set up File Management section with all fields
                config['File Management'] = {
                    'file_collection_management': file_management_type,
                    'original_files_path': original_files_path,
                    'symlinked_files_path': request.form.get('symlinked_files_path', '/mnt/symlinked'),
                    'symlink_organize_by_type': True,
                    'plex_url_for_symlink': plex_url,
                    'plex_token_for_symlink': plex_token
                }

                # Set up Plex section with all fields
                config['Plex'] = {
                    'url': plex_url,
                    'token': plex_token,
                    'shows_libraries': request.form.get('shows_libraries', ''),
                    'movie_libraries': request.form.get('movie_libraries', ''),
                    'update_plex_on_file_discovery': request.form.get('update_plex_on_file_discovery', 'false') == 'on',
                    'mounted_file_location': original_files_path
                }

                # Add required settings based on mode
                if file_management_type == 'Symlinked/Local':
                    required_settings.extend([
                        ('File Management', 'symlinked_files_path'),
                        ('File Management', 'original_files_path')
                    ])
                
                # Add Plex-specific required settings if we have Plex URL and token
                if plex_url and plex_token:
                    required_settings.extend([
                        ('Plex', 'url'),
                        ('Plex', 'token')
                    ])
                    if file_management_type == 'Plex':
                        required_settings.extend([
                            ('Plex', 'shows_libraries'),
                            ('Plex', 'movie_libraries')
                        ])
                
                # Handle debrid provider selection
                provider = request.form.get('debrid_provider', 'RealDebrid')
                api_key = request.form.get('debrid_api_key', '')
                
                config['Debrid Provider'] = {
                    'provider': provider,
                    'api_key': api_key
                }
                
                config['Trakt'] = {
                    'client_id': request.form['trakt_client_id'],
                    'client_secret': request.form['trakt_client_secret']
                }
                
                save_config(config)
                
                # Check if all required settings are now present
                can_proceed = all(get_setting(category, key) for category, key in required_settings)
                
                return jsonify({'success': True, 'can_proceed': can_proceed})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        # For GET requests, load existing settings if any
        config = load_config()
        can_proceed = all(get_setting(category, key) for category, key in required_settings)
        is_windows = platform.system() == 'Windows'  # Proper platform detection
        
        # Get Trakt auth status
        trakt_status = json.loads(check_trakt_auth_status().get_data(as_text=True))
        
        return render_template('onboarding_step_2.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               trakt_status=trakt_status,
                               is_onboarding=True,
                               is_windows=is_windows)

    elif step_num == 3:
        config = load_config()
        can_proceed = 'Scrapers' in config and bool(config['Scrapers'])
        return render_template('onboarding_step_3.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

    elif step_num == '3a':
        config = load_config()
        can_proceed = 'Scraping' in config and 'versions' in config['Scraping'] and bool(config['Scraping']['versions'])
        return render_template('onboarding_step_3a.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               is_onboarding=True)

    elif step_num == 4:
        config = load_config()
        can_proceed = 'Content Sources' in config and bool(config['Content Sources'])
        return render_template('onboarding_step_4.html', 
                               current_step=step_num, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

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
    
    if step in [1, 2, 3, '3a', 4]:
        session[f'onboarding_step_{step}_can_proceed'] = can_proceed
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid step'}), 400
    
def add_scraper_onboarding(form_data):
    scraper_type = form_data.get('scraper_type')
    scraper_config = {
        'enabled': True,
    }
    add_scraper(scraper_type, scraper_config)

def add_content_source_onboarding(form_data):
    source_type = form_data.get('source_type')
    source_config = {
        'enabled': True,
        'display_name': form_data.get('source_display_name'),
        'versions': form_data.getlist('source_versions')
    }
    add_content_source(source_type, source_config)

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
def add_onboarding_content_source():
    from routes.auth_routes import db

    data = request.json
    source_type = data.get('type')
    source_config = data.get('config')
    
    if not source_type or not source_config:
        return jsonify({'success': False, 'error': 'Invalid content source data'}), 400

    try:
        new_source_id = add_content_source(source_type, source_config)
        
        # Mark onboarding as complete
        current_user.onboarding_complete = True
        db.session.commit()

        # Log the addition of the new content source

        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@onboarding_bp.route('/content_sources/get', methods=['GET'])
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
def get_onboarding_scrapers():
    config = load_config()
    scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())
    return jsonify({
        'scrapers': config.get('Scrapers', {}),
        'scraper_types': scraper_types
    })

@onboarding_bp.route('/settings/api/update', methods=['POST'])
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
@login_required
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
        elif management_type in ['plex_direct', 'plex_symlink']:
            checks = []
            is_valid = True
            
            # Validate based on setup type
            if management_type == 'plex_direct':
                # Check for direct mount requirements
                mount_path = config.get('File Management', {}).get('original_files_path')
                if not mount_path:
                    is_valid = False
                    checks.append({
                        'name': 'Mount Configuration',
                        'valid': False,
                        'message': 'Direct mount path is not configured.'
                    })
                else:
                    checks.append({
                        'name': 'Mount Configuration',
                        'valid': True,
                        'message': 'Mount path is properly configured.'
                    })
                    
            elif management_type == 'plex_symlink':
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
                        'message': 'Symlink path is properly configured.'
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
