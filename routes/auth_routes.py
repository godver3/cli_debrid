from flask import Blueprint, redirect, url_for, flash, render_template, request, make_response, session, current_app as app
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
from sqlalchemy import inspect

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
    registration_key = db.Column(db.String(120), nullable=True)
    registration_key_limit = db.Column(db.Integer, nullable=True)  # New column for key usage limit
    registration_key_used = db.Column(db.Integer, default=0)  # New column for current usage count

    @staticmethod
    def get_registration_key():
        admin_user = User.query.filter_by(role='admin').first()
        if admin_user:
            return {
                'key': admin_user.registration_key,
                'limit': admin_user.registration_key_limit,
                'used': admin_user.registration_key_used
            }
        return None

    @staticmethod
    def set_registration_key(key, limit=None):
        admin_user = User.query.filter_by(role='admin').first()
        if admin_user:
            admin_user.registration_key = key
            if limit is not None:
                admin_user.registration_key_limit = limit
                admin_user.registration_key_used = 0  # Reset usage count when key/limit changes
            db.session.commit()
            return True
        return False

    @staticmethod
    def increment_key_usage():
        admin_user = User.query.filter_by(role='admin').first()
        if admin_user:
            admin_user.registration_key_used += 1
            db.session.commit()
            return True
        return False

def recreate_database():
    """Recreate the database with the new schema."""
    # Drop all tables
    db.drop_all()
    # Create all tables with new schema
    db.create_all()
    # Create default admin if no users exist
    create_default_admin()

def init_db(app):
    db.init_app(app)
    with app.app_context():
        # Create tables if they don't exist
        db.create_all()
        
        # Check if the columns exist
        inspector = inspect(db.engine)
        if 'user' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('user')]
            
            # Add registration_key column if it doesn't exist
            if 'registration_key' not in columns:
                with db.engine.connect() as conn:
                    conn.execute(db.text('ALTER TABLE user ADD COLUMN registration_key VARCHAR(120)'))
                    db.session.commit()
                logging.info("Added registration_key column to user table")
            
            # Add registration_key_limit column if it doesn't exist
            if 'registration_key_limit' not in columns:
                with db.engine.connect() as conn:
                    conn.execute(db.text('ALTER TABLE user ADD COLUMN registration_key_limit INTEGER'))
                    db.session.commit()
                logging.info("Added registration_key_limit column to user table")
            
            # Add registration_key_used column if it doesn't exist
            if 'registration_key_used' not in columns:
                with db.engine.connect() as conn:
                    conn.execute(db.text('ALTER TABLE user ADD COLUMN registration_key_used INTEGER DEFAULT 0'))
                    db.session.commit()
                logging.info("Added registration_key_used column to user table")
        
        # Create default admin if no users exist
        if User.query.count() == 0:
            create_default_admin()

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

    show_login_reminder = User.query.filter_by(is_default=False).count() == 0

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = bool(request.form.get('remember_me'))

        user = User.query.filter_by(username=username).first()
        if user:
            if check_password_hash(user.password, password):
                # Set session as permanent if remember me is checked
                if remember:
                    session.permanent = True  # This will use the PERMANENT_SESSION_LIFETIME value
                else:
                    session.permanent = False  # Session will expire when browser closes
                
                # Login the user with the remember flag
                login_user(user, remember=remember)
                
                # Force session save
                session.modified = True
                
                if not user.onboarding_complete:
                    response = redirect(url_for('onboarding.onboarding_step', step=1))
                else:
                    response = redirect(url_for('root.root'))
                
                return response
            
        flash('Please check your login details and try again.')
    
    return render_template('login.html', show_login_reminder=show_login_reminder)

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
    response = redirect(url_for('auth.login'))
    
    from extensions import get_root_domain
    # Explicitly clear cookies with matching domain
    domain = get_root_domain(request.host) if hasattr(request, 'host') else None
    response.set_cookie('session', '', expires=0, path='/', domain=domain)
    response.set_cookie('remember_token', '', expires=0, path='/', domain=domain)
    
    return response

@auth_bp.route('/unauthorized')
def unauthorized():
    flash('You are not authorized to access this page.', 'error')
    return redirect(url_for('auth.login'))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    
    if current_user.is_authenticated:
        return redirect(url_for('root.root'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        registration_key = request.form['registration_key']

        # Validate registration key
        key_info = User.get_registration_key()
        if not key_info or registration_key != key_info['key']:
            flash('Invalid registration key.', 'error')
            return redirect(url_for('auth.login'))

        # Check key usage limit
        if key_info['limit'] is not None and key_info['used'] >= key_info['limit']:
            flash('Registration key has reached its usage limit.', 'error')
            return redirect(url_for('auth.login'))

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Username already exists.', 'error')
            return redirect(url_for('auth.login'))

        hashed_password = generate_password_hash(password)
        new_user = User(
            username=username, 
            password=hashed_password, 
            role='user',  # New users are always regular users
            onboarding_complete=True
        )
        db.session.add(new_user)
        
        # Increment key usage count
        User.increment_key_usage()
        
        db.session.commit()
        login_user(new_user)
        flash('Registered successfully.', 'success')
        return redirect(url_for('root.root'))
    return redirect(url_for('auth.login'))

@auth_bp.route('/account')
@login_required
def account():
    if current_user.role == 'admin':
        return redirect(url_for('root.root'))
    return render_template('account.html')

@auth_bp.route('/change_password', methods=['POST'])
@login_required
def change_password():
    if current_user.role == 'admin':
        return redirect(url_for('root.root'))

    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    # Verify current password
    if not check_password_hash(current_user.password, current_password):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('auth.account'))

    # Verify new password match
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('auth.account'))

    # Update password
    current_user.password = generate_password_hash(new_password)
    current_user.is_default = False
    db.session.commit()

    flash('Password changed successfully.', 'success')
    return redirect(url_for('auth.account'))