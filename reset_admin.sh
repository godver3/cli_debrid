#!/bin/bash

# Navigate to the project directory
cd "$(dirname "$0")"

# Run a Python script to reset the admin account
python3 << END
from web_server import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    # Delete all existing users
    User.query.delete()
    db.session.commit()
    
    # Create new admin user
    hashed_password = generate_password_hash('admin')
    new_admin = User(username='admin', password=hashed_password, role='admin', is_default=True)
    db.session.add(new_admin)
    db.session.commit()
    print("Admin account reset successfully.")
END