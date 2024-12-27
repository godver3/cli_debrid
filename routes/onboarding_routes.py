from flask import Blueprint, jsonify, request, render_template, redirect, url_for, flash, abort, session
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash
from settings import load_config, get_setting, save_config
from settings_schema import SETTINGS_SCHEMA
from config_manager import add_scraper, add_content_source, load_config
import logging
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
        ('RealDebrid', 'api_key'),
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
    
    # Step 4: Check if at least one content source is configured
    if 'Content Sources' not in config or not config['Content Sources']:
        return 4
    
    # If all steps are completed, return the final step (5)
    return 5

@onboarding_bp.route('/')
@login_required
def onboarding():
    return render_template('onboarding.html', is_onboarding=True)

@onboarding_bp.route('/step/<int:step>', methods=['GET', 'POST'])
@login_required
def onboarding_step(step):
    from routes.auth_routes import db

    if step < 1 or step > 5:
        abort(404)
    
    config = load_config()
    can_proceed = False

    if step == 1:
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
                    return jsonify({'success': True})
                except Exception as e:
                    return jsonify({'success': False, 'error': str(e)})
            else:
                return jsonify({'success': False, 'error': 'Passwords do not match'})

        return render_template('onboarding_step_1.html', 
                               current_step=step, 
                               can_proceed=can_proceed, 
                               admin_created=admin_created, 
                               is_onboarding=True)
       
    if step == 2:
        required_settings = [
            ('Plex', 'url'),
            ('Plex', 'token'),
            ('Plex', 'shows_libraries'),
            ('Plex', 'movie_libraries'),
            ('RealDebrid', 'api_key'),
            ('Trakt', 'client_id'),
            ('Trakt', 'client_secret')
        ]

        if request.method == 'POST':
            try:
                config = load_config()
                config['Plex'] = {
                    'url': request.form['plex_url'],
                    'token': request.form['plex_token'],
                    'shows_libraries': request.form['shows_libraries'],
                    'movie_libraries': request.form['movie_libraries']
                }
                config['RealDebrid'] = {
                    'api_key': request.form['realdebrid_api_key']
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
        
        return render_template('onboarding_step_2.html', 
                               current_step=step, 
                               can_proceed=can_proceed,
                               settings=config, is_onboarding=True)
    if step == 3:
        config = load_config()
        can_proceed = 'Scrapers' in config and bool(config['Scrapers'])
        return render_template('onboarding_step_3.html', 
                               current_step=step, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

    if step == 4:
        config = load_config()
        can_proceed = 'Content Sources' in config and bool(config['Content Sources'])
        return render_template('onboarding_step_4.html', 
                               current_step=step, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

    elif step == 5:
        can_proceed = True  # Always allow finishing the onboarding process
        return render_template('onboarding_step_5.html', current_step=step, can_proceed=can_proceed, is_onboarding=True)


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
    
    if step in [1, 2, 3, 4]:
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