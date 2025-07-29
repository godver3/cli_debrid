#!/bin/bash

# Navigate to the project directory
cd "$(dirname "$0")"

# Default action
ACTION=""

# Parse arguments
if [ "$1" == "--reset-all" ]; then
    ACTION="reset_all"
elif [ "$1" == "--reset-admin-password" ]; then
    ACTION="reset_admin_password"
else
    echo "Usage: $0 [--reset-all | --reset-admin-password]"
    echo "  --reset-all             Deletes all users and creates a new default admin (admin/admin)."
    echo "  --reset-admin-password  Resets the password for all existing admin accounts to 'admin'."
    exit 1
fi

# Run a Python script to perform the action
python3 -c """
from web_server import app, db
from routes.auth_routes import User
from werkzeug.security import generate_password_hash
import sys

action = '$ACTION'

with app.app_context():
    if action == 'reset_all':
        # Delete all existing users
        User.query.delete()
        db.session.commit()
        
        # Create new admin user
        hashed_password = generate_password_hash('admin')
        new_admin = User(username='admin', password=hashed_password, role='admin', is_default=True)
        db.session.add(new_admin)
        db.session.commit()
        print("All users deleted and admin account reset to admin/admin successfully.")
    
    elif action == 'reset_admin_password':
        admin_users = User.query.filter_by(role='admin').all()
        
        if admin_users:
            updated_count = 0
            for user in admin_users:
                user.password = generate_password_hash('admin')
                print(f"Password for admin user '{user.username}' has been reset to 'admin'.")
                updated_count += 1
            db.session.commit()
            print("\nSuccessfully reset passwords for all admin users.")
        else:
            print("No admin users found to reset.")
    else:
        print("Invalid action specified to Python script.")
"""
