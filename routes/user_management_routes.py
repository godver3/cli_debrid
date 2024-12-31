from flask import Blueprint, jsonify, request, render_template, redirect, url_for, flash
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash
from extensions import db
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
            return redirect(url_for('root.root'))
        else:
            flash('Passwords do not match.', 'error')
    return render_template('change_password.html')

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
    username = request.form['username']
    password = request.form['password']
    role = request.form['role']
    
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
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    if user.username == 'admin':
        return jsonify({'success': False, 'error': 'Cannot delete admin user'}), 400

    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Database error'}), 500

# Modify the register route
@user_management_bp.route('/register', methods=['GET', 'POST'])
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
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        flash('Registered successfully.', 'success')
        return redirect(url_for('root.root'))
    return render_template('register.html')