from flask import Blueprint, redirect, url_for, flash, render_template, request, make_response, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
import logging
from utilities.settings import load_config
from routes.onboarding_routes import get_next_onboarding_step
from routes.extensions import db, login_manager
from .utils import is_user_system_enabled
import random
import string
from routes.poster_cache import load_cache, UNAVAILABLE_POSTER

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

def create_default_admin():
    """Create default admin user if no users exist."""
    if User.query.count() == 0:
        # Check environment variables first
        username = os.environ.get('DEFAULT_ADMIN_USER', 'admin')
        password = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'admin')
        skip_onboarding = os.environ.get('DISABLE_ONBOARDING', '').lower() in ('true', '1', 'yes')

        logging.info(f"Creating default admin user '{username}'")
        logging.info(f"Skip onboarding: {skip_onboarding}")
        logging.info(f"Default admin password: {password}")
        
        # If no password specified, generate a random one
        if not password:
            password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
            logging.info(f"Generated random password for default admin: {password}")
        
        hashed_password = generate_password_hash(password)
        
        default_admin = User(
            username=username,
            password=hashed_password, 
            role='admin', 
            is_default=True,
            onboarding_complete=skip_onboarding
        )
        db.session.add(default_admin)
        db.session.commit()
        
        logging.debug(f"DISABLE_ONBOARDING value: {os.environ.get('DISABLE_ONBOARDING')}")
        if skip_onboarding:
            logging.info(f"Created default admin user '{username}' with onboarding disabled")
        else:
            logging.info(f"Created default admin user '{username}' - onboarding required")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    onboarding_complete = db.Column(db.Boolean, default=False)

def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()

@login_manager.user_loader
def load_user(user_id):
    from routes.settings_routes import is_user_system_enabled
    
    if not is_user_system_enabled():
        return None
    
    user = User.query.get(int(user_id))

    return user

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))

    if current_user.is_authenticated:
        return redirect(url_for('root.root'))

    posters_data = []
    try:
        cache = load_cache()
        if cache:
            all_urls = [item[0] for item in cache.values() if item[0] != UNAVAILABLE_POSTER]
            if all_urls:
                random.shuffle(all_urls)
                poster_urls = all_urls[:20]

                placed_posters = []
                # Approximate aspect ratio of a poster (width / height)
                poster_aspect_ratio = 27/40 
                # Approximate viewport aspect ratio (width / height) to convert vh to vw
                viewport_aspect_ratio = 16/9 
                vh_to_vw = 1 / viewport_aspect_ratio

                for i, url in enumerate(poster_urls):
                    placed = False
                    for _ in range(100):  # Attempts to place a poster
                        # Generate attributes
                        poster_height_vh = random.uniform(30, 60)
                        poster_width_vh = poster_height_vh * poster_aspect_ratio
                        poster_width_vw = poster_width_vh * vh_to_vw
                        
                        poster_top_vh = random.uniform(-10, 90)
                        
                        animation_duration = ((poster_height_vh * poster_height_vh / 12) + 15) / 3
                        animation_delay = -random.uniform(0, 120)
                        animation_name = 'float-lr' if i % 2 != 0 else 'float-rl'
                        opacity = random.uniform(0.5, 0.8)

                        # Calculate position at t=0
                        progress = (-animation_delay / animation_duration) % 1.0

                        if animation_name == 'float-lr':
                            left_vw = -poster_width_vw + (100 + poster_width_vw) * progress
                        else: # float-rl
                            left_vw = 100 - (100 + poster_width_vw) * progress

                        new_rect = {
                            'left': left_vw, 
                            'right': left_vw + poster_width_vw, 
                            'top': poster_top_vh, 
                            'bottom': poster_top_vh + poster_height_vh
                        }

                        # Check for significant overlap with already placed posters
                        has_significant_overlap = False
                        for p in placed_posters:
                            existing_rect = p['rect']
                            
                            overlap_x = max(0, min(new_rect['right'], existing_rect['right']) - max(new_rect['left'], existing_rect['left']))
                            overlap_y = max(0, min(new_rect['bottom'], existing_rect['bottom']) - max(new_rect['top'], existing_rect['top']))
                            
                            if overlap_x > 0 and overlap_y > 0:
                                overlap_area = overlap_x * overlap_y
                                new_area = poster_width_vw * poster_height_vh
                                if overlap_area > 0.15 * new_area:
                                    has_significant_overlap = True
                                    break

                        if not has_significant_overlap:
                            style = (
                                f"top: {poster_top_vh:.2f}vh; "
                                f"height: {poster_height_vh:.2f}vh; "
                                f"opacity: {opacity:.2f}; "
                                f"animation-name: {animation_name}; "
                                f"animation-duration: {animation_duration:.2f}s; "
                                f"animation-delay: {animation_delay:.2f}s;"
                            )
                            placed_posters.append({'url': url, 'style': style, 'rect': new_rect})
                            placed = True
                            break
                    if not placed:
                        # logging.debug(f"Could not place poster for URL: {url} after 100 attempts.")
                        pass
                
                posters_data = [{'url': p['url'], 'style': p['style']} for p in placed_posters]
    except Exception as e:
        logging.error(f"Failed to load poster cache for login background: {e}")

    show_login_reminder_flag = User.query.filter_by(is_default=False).count() == 0
    
    from routes.extensions import get_root_domain
    domain = get_root_domain(request.host) if hasattr(request, 'host') else None

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = bool(request.form.get('remember_me'))
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session.clear() 
            session.permanent = True
            login_user(user, remember=remember)
            session.modified = True
            
            if not user.onboarding_complete:
                response = make_response(redirect(url_for('onboarding.onboarding_step', step=1)))
            else:
                response = make_response(redirect(url_for('root.root')))
            
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
            
        flash('Please check your login details and try again.', 'error')
        response = make_response(render_template('login.html', show_login_reminder=show_login_reminder_flag, posters=posters_data))
        response.set_cookie('session', '', expires=0, path='/', domain=domain)
        response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
        return response

    response = make_response(render_template('login.html', show_login_reminder=show_login_reminder_flag, posters=posters_data))
    response.set_cookie('session', '', expires=0, path='/', domain=domain)
    response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
    return response

@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    
    session.clear()
    
    logout_user()
        
    response = make_response(redirect(url_for('auth.login')))
    
    from routes.extensions import get_root_domain
    domain = get_root_domain(request.host) if hasattr(request, 'host') else None
    
    response.set_cookie('session', '', expires=0, path='/', domain=domain)
    response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
    response.set_cookie('session', '', expires=0, path='/')
    response.set_cookie('remember_token', '', expires=0, path='/')
    
    response.headers['Cache-control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@auth_bp.route('/unauthorized')
def unauthorized():
    if current_user.is_authenticated and current_user.role == 'requester':
        flash('As a requester, you can only request content but not scrape directly. Please use the content request feature.', 'error')
        return redirect(url_for('content.index'))
    else:
        flash('You are not authorized to access this page.', 'error')
        return redirect(url_for('auth.login'))