from flask import Blueprint, redirect, url_for, flash, render_template, request
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
import logging
from settings import load_config
from routes.onboarding_routes import get_next_onboarding_step
from extensions import db, login_manager

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
                onboarding_complete=False  # Set onboarding_complete to False
            )
            db.session.add(default_admin)
            db.session.commit()
            logging.info("Default admin account created with onboarding incomplete.")
        else:
            logging.info("Default admin already exists.")
    else:
        logging.info("Users already exist. Skipping default admin creation.")

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
    from routes.settings_routes import is_user_system_enabled
    
    if not is_user_system_enabled():
        return redirect(url_for('statistics.index'))

    if current_user.is_authenticated:
        if not current_user.onboarding_complete:
            return redirect(url_for('onboarding.onboarding_step', step=1))
        return redirect(url_for('statistics.index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            if user.is_default or not user.onboarding_complete:
                return redirect(url_for('onboarding.onboarding_step', step=1))
            return redirect(url_for('statistics.index'))
        else:
            flash('Please check your login details and try again.')
    
    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    from routes.settings_routes import is_user_system_enabled

    if not is_user_system_enabled():
        return redirect(url_for('statistics.index'))  # Update this line if needed
    logout_user()
    return redirect(url_for('auth.login'))

@auth_bp.route('/unauthorized')
def unauthorized():
    flash('You are not authorized to access this page.', 'error')
    return redirect(url_for('auth.login'))  # Update this line