from flask import Blueprint, redirect, url_for, flash, render_template, request, make_response, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
import logging
from settings import load_config
from routes.onboarding_routes import get_next_onboarding_step
from extensions import db, login_manager
from .utils import is_user_system_enabled
import random
import string

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

    # If already authenticated, redirect to root
    if current_user.is_authenticated:
        return redirect(url_for('root.root'))

    # Clear any existing cookies before processing login
    response = make_response(render_template('login.html', 
        show_login_reminder=User.query.filter_by(is_default=False).count() == 0))
    
    from extensions import get_root_domain
    domain = get_root_domain(request.host) if hasattr(request, 'host') else None
    
    # Clear existing cookies on GET request to ensure clean state
    if request.method == 'GET':
        response.set_cookie('session', '', expires=0, path='/', domain=domain)
        response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
        return response

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = bool(request.form.get('remember_me'))
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            # Clear session first
            session.clear()
            
            # Always set session as permanent
            session.permanent = True
            
            login_user(user, remember=remember)
            
            # Force session save
            session.modified = True
            
            if not user.onboarding_complete:
                response = make_response(redirect(url_for('onboarding.onboarding_step', step=1)))
            else:
                response = make_response(redirect(url_for('root.root')))
            
            # Add security headers
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            
            return response
            
        flash('Please check your login details and try again.')
        
    return response

@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    
    # Clear the session
    session.clear()
    
    # Perform Flask-Login logout
    logout_user()
        
    # Create response with redirect
    response = make_response(redirect(url_for('auth.login')))
    
    from extensions import get_root_domain
    # Get domain for cookie cleanup
    domain = get_root_domain(request.host) if hasattr(request, 'host') else None
    
    # Clear all possible variations of cookies
    response.set_cookie('session', '', expires=0, path='/', domain=domain)
    response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
    response.set_cookie('session', '', expires=0, path='/')  # Also clear without domain
    response.set_cookie('remember_token', '', expires=0, path='/')
    
    # Add cache control headers
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
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