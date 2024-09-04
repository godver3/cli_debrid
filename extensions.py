from flask_sqlalchemy import SQLAlchemy
from flask import Flask, redirect, request
from flask_login import LoginManager
import time
from sqlalchemy import inspect

db = SQLAlchemy()
app = Flask(__name__)

app.config['PREFERRED_URL_SCHEME'] = 'https'

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

app_start_time = time.time()

def update_stats(processed=0, successful=0, failed=0):
    global total_processed, successful_additions, failed_additions
    total_processed += processed
    successful_additions += successful
    failed_additions += failed

def initialize_app():
    from routes.auth_routes import create_default_admin

    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table("user"):
            db.create_all()
        else:
            columns = [c['name'] for c in inspector.get_columns('user')]
            if 'is_default' not in columns:
                with db.engine.connect() as conn:
                    conn.execute(db.text('ALTER TABLE user ADD COLUMN is_default BOOLEAN'))
                    conn.commit()
        create_default_admin()

@app.before_request
def force_https():
    if request.headers.get('X-Forwarded-Proto') == 'http':
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

@app.after_request
def add_security_headers(response):
    response.headers['Content-Security-Policy'] = "upgrade-insecure-requests"
    return response