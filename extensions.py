from flask_sqlalchemy import SQLAlchemy
from flask import Flask, redirect, request, jsonify
from flask_login import LoginManager
import time
from sqlalchemy import inspect
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Add this line

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'

app_start_time = time.time()

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

def is_behind_proxy():
    return request.headers.get('X-Forwarded-Proto') is not None

@app.before_request
def handle_https():
    if is_behind_proxy():
        if request.headers.get('X-Forwarded-Proto') == 'http':
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)
    # Remove the else clause to avoid redirecting HTTPS to HTTP

@app.after_request
def add_security_headers(response):
    if is_behind_proxy():
        response.headers['Content-Security-Policy'] = "upgrade-insecure-requests"
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Add CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    
    return response

# Add an error handler for JSON parsing
@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request", "message": str(error)}), 400