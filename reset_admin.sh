#!/bin/bash

# Navigate to the project directory
cd "$(dirname "$0")"

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

DB_PATH="${USER_DB_CONTENT:-/user/db_content}/users.db"

if [ ! -f "$DB_PATH" ]; then
    echo "Error: users.db not found at $DB_PATH"
    exit 1
fi

python3 << PYEOF
import sqlite3
from werkzeug.security import generate_password_hash

db_path = "$DB_PATH"
action = "$ACTION"

conn = sqlite3.connect(db_path)
cur = conn.cursor()

if action == "reset_all":
    cur.execute("DELETE FROM user")
    hashed = generate_password_hash("admin")
    cur.execute(
        "INSERT INTO user (username, password, role, is_default) VALUES (?, ?, ?, ?)",
        ("admin", hashed, "admin", 1)
    )
    conn.commit()
    print("All users deleted and admin account reset to admin/admin successfully.")

elif action == "reset_admin_password":
    cur.execute("SELECT id, username FROM user WHERE role = 'admin'")
    admins = cur.fetchall()
    if admins:
        hashed = generate_password_hash("admin")
        for user_id, username in admins:
            cur.execute("UPDATE user SET password = ? WHERE id = ?", (hashed, user_id))
            print("Password for admin user '{}' has been reset to 'admin'.".format(username))
        conn.commit()
        print("\nSuccessfully reset passwords for all admin users.")
    else:
        print("No admin users found to reset.")

conn.close()
PYEOF
