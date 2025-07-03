from flask import Blueprint, jsonify, request, render_template, redirect, url_for, flash
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from routes.extensions import db
from .auth_routes import User
from .models import admin_required, onboarding_required
from .settings_routes import is_user_system_enabled

user_management_bp = Blueprint('user_management', __name__)

@user_management_bp.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        if new_password == confirm_password:
            current_user.password = generate_password_hash(new_password)
            current_user.is_default = False
            db.session.commit()
            flash('Password changed successfully.', 'success')
            if current_user.role == 'admin' and 'manage_users' in request.referrer:
                return redirect(url_for('user_management.manage_users'))
            return redirect(url_for('root.root'))
        else:
            flash('Passwords do not match.', 'error')
    return render_template('change_password.html')

@user_management_bp.route('/change_own_password', methods=['POST'])
@login_required
def change_own_password():
    data = request.get_json()
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not new_password or not confirm_password:
        return jsonify({'success': False, 'error': 'New password and confirmation are required.'}), 400

    if new_password != confirm_password:
        return jsonify({'success': False, 'error': 'Passwords do not match.'}), 400

    try:
        current_user.password = generate_password_hash(new_password)
        current_user.is_default = False
        db.session.commit()
        return jsonify({'success': True, 'message': 'Password changed successfully.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'An error occurred while changing the password.'}), 500

# Modify the manage_users route
@user_management_bp.route('/manage_users')
@admin_required
@onboarding_required
def manage_users():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    users = User.query.all()
    return render_template('manage_users.html', users=users)

@user_management_bp.route('/add_user', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    role = request.form.get('role')
    
    if not all([username, password, role]):
        return jsonify({'success': False, 'error': 'Username, password, and role are required.'})

    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        return jsonify({'success': False, 'error': 'Username already exists.'})
    else:
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, role=role, onboarding_complete=True)
        db.session.add(new_user)
        db.session.commit()
        return jsonify({'success': True})

@user_management_bp.route('/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    if not current_user.is_authenticated or current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    # Count number of admin users
    admin_count = User.query.filter_by(role='admin').count()

    # If trying to delete an admin and they're the last one, prevent it
    if user.role == 'admin' and admin_count <= 1:
        return jsonify({'success': False, 'error': 'Cannot delete the last admin account'}), 400

    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Database error'}), 500

# Modify the register route
@user_management_bp.route('/register', methods=['GET', 'POST'])
@admin_required
def register():
    if not is_user_system_enabled():
        return redirect(url_for('root.root'))
    
    if current_user.is_authenticated:
        return redirect(url_for('root.root'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, onboarding_complete=True)
        if User.query.count() == 0:
            new_user.role = 'admin'
        else:
            new_user.role = 'user'  # Default role for new users
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        flash('Registered successfully.', 'success')
        return redirect(url_for('root.root'))
    return render_template('register.html')