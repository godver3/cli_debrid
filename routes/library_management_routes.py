from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from functools import wraps
import os
from pathlib import Path
from utilities.local_library_scan import scan_for_broken_symlinks, repair_broken_symlink

library_management = Blueprint('library_management', __name__)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated_function

@library_management.route('/library-management')
@login_required
@admin_required
def manage_libraries():
    return render_template('library_management.html')

@library_management.route('/api/libraries', methods=['GET'])
@login_required
@admin_required
def get_libraries():
    # TODO: Implement logic to get all configured libraries
    libraries = []  # This will be populated with actual library data
    return jsonify(libraries)

@library_management.route('/api/libraries', methods=['POST'])
@login_required
@admin_required
def create_library():
    data = request.get_json()
    # TODO: Implement library creation logic
    return jsonify({'message': 'Library created successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['PUT'])
@login_required
@admin_required
def update_library(library_id):
    data = request.get_json()
    # TODO: Implement library update logic
    return jsonify({'message': 'Library updated successfully'})

@library_management.route('/api/libraries/<library_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_library(library_id):
    # TODO: Implement library deletion logic
    return jsonify({'message': 'Library deleted successfully'})

@library_management.route('/api/libraries/verify', methods=['POST'])
@login_required
@admin_required
def verify_library_path():
    data = request.get_json()
    path = data.get('path')
    
    if not path:
        return jsonify({'error': 'Path is required'}), 400
        
    path_obj = Path(path)
    exists = path_obj.exists()
    is_dir = path_obj.is_dir() if exists else False
    is_symlink = path_obj.is_symlink() if exists else False
    
    return jsonify({
        'exists': exists,
        'is_directory': is_dir,
        'is_symlink': is_symlink,
        'valid': exists and is_dir
    })

@library_management.route('/api/libraries/scan-broken', methods=['POST'])
@login_required
@admin_required
def scan_broken_symlinks():
    """Scan for broken symlinks in the library."""
    data = request.get_json()
    library_path = data.get('path') if data else None
    
    results = scan_for_broken_symlinks(library_path)
    return jsonify(results)

@library_management.route('/api/libraries/repair-symlink', methods=['POST'])
@login_required
@admin_required
def repair_symlink():
    """Attempt to repair a broken symlink."""
    data = request.get_json()
    
    if not data or 'symlink_path' not in data:
        return jsonify({'error': 'Symlink path is required'}), 400
        
    symlink_path = data.get('symlink_path')
    new_target_path = data.get('new_target_path')  # Optional
    
    result = repair_broken_symlink(symlink_path, new_target_path)
    return jsonify(result) 