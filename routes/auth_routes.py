from flask import Blueprint, redirect, url_for, flash, render_template, request, make_response
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

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

def create_default_admin():
    # Check if there are any existing users
    if User.query.count() == 0:
        default_admin = User.query.filter_by(username='admin').first()
        if not default_admin:
            hashed_password = generate_password_hash('admin')
            default_admin = User(
                username='admin', 
                password=hashed_password, 
                role='admin', 
                is_default=True,
                onboarding_complete=False  # Set to False for first-time onboarding
            )
            db.session.add(default_admin)
            db.session.commit()
            logging.info("Default admin account created with onboarding required.")
        else:
            logging.info("Default admin already exists.")

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
    return User.query.get(int(user_id))

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    logging.debug("Entering login route")
    logging.debug(f"Request method: {request.method}")
    
    if not is_user_system_enabled():
        logging.debug("User system not enabled, redirecting to root.root")
        return redirect(url_for('root.root'))

    # If already authenticated, clear any existing remember token if it wasn't explicitly requested
    if current_user.is_authenticated:
        logging.debug("User already authenticated, redirecting to root.root")
        from flask import session
        if 'remember_token' in request.cookies and 'remember_me' not in session:
            response = redirect(url_for('root.root'))
            response.delete_cookie('remember_token')
            return response
        return redirect(url_for('root.root'))

    # Check if there are any non-default users
    show_login_reminder = User.query.filter_by(is_default=False).count() == 0

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        logging.debug(f"Login attempt for username: {username}")
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            logging.debug("Password check passed, logging in user")
            remember = 'remember_me' in request.form
            
            # Get the domain for cookies
            host = request.host.split(':')[0]
            domain_parts = host.split('.')
            if len(domain_parts) > 2:
                domain = '.' + '.'.join(domain_parts[-2:])
            else:
                domain = '.' + host
                
            from extensions import app
            from flask import session
            
            # Set session cookie domain
            app.config['SESSION_COOKIE_DOMAIN'] = domain
            app.config['REMEMBER_COOKIE_DOMAIN'] = domain
            
            # Store remember preference in session
            if remember:
                session['remember_me'] = True
            
            login_user(user, remember=remember)
            logging.debug(f"User ID in session: {current_user.get_id()}")
            logging.debug(f"Session contents: {dict(session)}")
            
            # Create the response first
            if not user.onboarding_complete:
                logging.debug("Redirecting to onboarding")
                response = redirect(url_for('onboarding.onboarding_step', step=1))
            else:
                logging.debug("Redirecting to root")
                response = redirect(url_for('root.root'))
            
            # Log the response cookies
            logging.debug(f"Response cookies: {response.headers.getlist('Set-Cookie')}")
            
            # Ensure session cookie is set
            if 'session' not in [c.split('=')[0].strip() for c in response.headers.getlist('Set-Cookie')]:
                response.set_cookie(
                    'session',
                    session.get('_id', ''),
                    domain=domain,
                    secure=True,
                    httponly=True,
                    samesite='None'
                )
            
            # If remember me wasn't checked, ensure no remember token is set
            if not remember:
                response.delete_cookie('remember_token', domain=domain, path='/')
            
            return response
        else:
            logging.debug("Login failed - invalid credentials")
            flash('Please check your login details and try again.')
    else:
        # On GET request, ensure no remember token exists
        response = make_response(render_template('login.html', show_login_reminder=show_login_reminder))
        response.delete_cookie('remember_token')
        return response
    
    return render_template('login.html', show_login_reminder=show_login_reminder)

@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    from routes.settings_routes import is_user_system_enabled
    from flask import make_response, session
    import logging

    logging.debug("Starting logout process")
    logging.debug(f"Current session: {dict(session)}")
    logging.debug(f"Current cookies: {request.cookies}")

    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    
    # Clear Flask session
    session.clear()
    
    # Perform Flask-Login logout
    logout_user()
    
    logging.debug("After logout_user and session clear")
    
    # Create response for redirect
    response = make_response(redirect(url_for('auth.login')))
    
    # Get the domain from the request host
    host = request.host.split(':')[0]  # Remove port if present
    domain_parts = host.split('.')
    if len(domain_parts) > 2:
        domain = '.' + '.'.join(domain_parts[-2:])
    else:
        domain = '.' + host  # e.g., .localhost
    
    # Clear session and remember token with all domain variations
    cookies_to_clear = ['session', 'remember_token']
    paths = ['/', '/auth', '/auth/']
    
    # Clear with domain
    if domain:
        for cookie in cookies_to_clear:
            for path in paths:
                # Clear with dot prefix
                response.delete_cookie(cookie, domain=domain, path=path)
                # Clear without dot prefix
                response.delete_cookie(cookie, domain=domain.lstrip('.'), path=path)
    
    # Clear without domain (for local development and fallback)
    for cookie in cookies_to_clear:
        for path in paths:
            response.delete_cookie(cookie, path=path)
    
    # Set secure attributes for all cleared cookies
    for cookie in cookies_to_clear:
        response.set_cookie(
            cookie,
            '',
            expires=0,
            secure=True,
            httponly=True,
            samesite='None',
            path='/'
        )
    
    logging.debug("Final response cookies:")
    logging.debug(response.headers.getlist('Set-Cookie'))
    
    return response

@auth_bp.route('/unauthorized')
def unauthorized():
    flash('You are not authorized to access this page.', 'error')
    return redirect(url_for('auth.login'))