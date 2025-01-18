from flask import Flask, redirect, request, jsonify, url_for
from flask_login import LoginManager
import time
from sqlalchemy import inspect
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import current_user
import logging
from routes.utils import is_user_system_enabled
from flask_cors import CORS
import threading
import uuid
from datetime import timedelta
import os

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def get_root_domain(host):
    """Get the root domain from a hostname."""
    if not host or host.lower() in ('localhost', '127.0.0.1', '::1'):
        return None
    # Remove port if present
    domain = host.split(':')[0]
    # If IP address, return as is
    if domain.replace('.', '').isdigit():
        return domain
    # For hostnames, get root domain with leading dot
    parts = domain.split('.')
    if len(parts) > 2:
        return '.' + '.'.join(parts[-2:])  # e.g., .example.com for sub.example.com
    return '.' + domain  # e.g., .localhost

class SameSiteMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            new_headers = []
            root_domain = get_root_domain(environ.get('HTTP_HOST', ''))
            # logger.debug(f"SameSiteMiddleware processing headers for domain: {root_domain}")
            
            for name, value in headers:
                if name.lower() == 'set-cookie':
                    # logger.debug(f"Processing cookie header: {value}")
                    # Parse the cookie
                    parts = [p.strip() for p in value.split(';')]
                    cookie_main = parts[0]
                    cookie_attrs = {
                        p.split('=')[0].lower(): p for p in parts[1:]
                        if '=' in p or p.lower() in ['secure', 'httponly']
                    }
                    
                    # Always set SameSite=None for session cookies
                    if cookie_main.startswith('session='):
                        cookie_attrs['samesite'] = 'SameSite=None'
                        cookie_attrs['secure'] = 'Secure'
                        if root_domain:
                            cookie_attrs['domain'] = f'Domain={root_domain}'
                        cookie_attrs['path'] = 'Path=/'
                        
                        # Reconstruct the cookie
                        value = '; '.join([cookie_main] + list(cookie_attrs.values()))
                        # logger.debug(f"Modified session cookie: {value}")
                    
                new_headers.append((name, value))
            
            return start_response(status, new_headers, exc_info)
        
        return self.app(environ, custom_start_response)

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
app = Flask(__name__)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Add SameSiteMiddleware to the WSGI stack
app.wsgi_app = SameSiteMiddleware(app.wsgi_app)

# Configure session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
app.config['SESSION_FILE_DIR'] = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'flask_session')
app.config['SESSION_FILE_THRESHOLD'] = 500
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['REMEMBER_COOKIE_SECURE'] = True
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'None'

# Use a persistent secret key
secret_key_file = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'secret_key')
if os.path.exists(secret_key_file):
    with open(secret_key_file, 'rb') as f:
        app.secret_key = f.read()
else:
    app.secret_key = os.urandom(24)
    with open(secret_key_file, 'wb') as f:
        f.write(app.secret_key)

# Initialize Flask-Session
from flask_session import Session
sess = Session()
sess.init_app(app)

# Configure CORS
CORS(app, resources={r"/*": {
    "origins": ["*"],
    "methods": ["GET", "HEAD", "POST", "OPTIONS", "PUT", "DELETE"],
    "allow_headers": ["*"],
    "supports_credentials": True,
    "expose_headers": ["Set-Cookie"],
    "max_age": 3600
}})

from flask_login import LoginManager
from flask import redirect, url_for
from functools import wraps

login_manager = LoginManager()

def init_login_manager(app):
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.refresh_view = 'auth.login'
    login_manager.needs_refresh_message = 'Please log in again to confirm your identity'
    login_manager.needs_refresh_message_category = 'info'
    login_manager.session_protection = 'strong'  # Use strong session protection
    
    def login_required(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            from routes.settings_routes import is_user_system_enabled
            if not is_user_system_enabled():
                return func(*args, **kwargs)
            if not login_manager._login_disabled:
                if not login_manager.current_user.is_authenticated:
                    return login_manager.unauthorized()
            return func(*args, **kwargs)
        return decorated_view

    login_manager.login_required = login_required

# Call this function in your app initialization
# init_login_manager(app)
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
    # Skip HTTPS redirect for webhook routes
    if request.path.startswith('/webhook'):
        return
        
    if request.headers.get('X-Forwarded-Proto') == 'http':
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

@app.before_request
def check_user_system():
    # Exclude the webhook route, its subpaths, and static files
    if request.path.startswith('/webhook') or request.path.startswith('/static') or request.path.startswith('/debug'):
        return    

    # Remove any specific handling for root.root here
    # The decorators will handle the logic now

@app.after_request
def add_security_headers(response):
    """Add security headers and handle cookies"""
    logging.debug("[login_testing] Processing request for URL: %s", request.url)
    logging.debug("[login_testing] Request cookies: %s", dict(request.cookies))
    logging.debug("[login_testing] Request headers: %s", dict(request.headers))
    
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    # Set cookie domain if needed
    if 'Set-Cookie' in response.headers:
        root_domain = get_root_domain(request.host)
        logging.debug("[login_testing] Setting cookie domain to: %s", root_domain)
        if root_domain:
            app.config['SESSION_COOKIE_DOMAIN'] = root_domain
            app.config['REMEMBER_COOKIE_DOMAIN'] = root_domain
            logging.debug("[login_testing] Cookie headers before: %s", response.headers.getlist('Set-Cookie'))
    
    # Handle CORS for the actual request
    if request.method != 'OPTIONS':
        origin = request.headers.get('Origin')
        if origin:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            logging.debug("[login_testing] Setting CORS headers for origin: %s", origin)
    
    logging.debug("[login_testing] Final response headers: %s", dict(response.headers))
    logging.debug("[login_testing] Final cookies: %s", response.headers.getlist('Set-Cookie'))
    return response

# Add an error handler for JSON parsing
@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request", "message": str(error)}), 400

class SimpleTaskQueue:
    def __init__(self):
        self.tasks = {}

    def add_task(self, func, *args, **kwargs):
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = {'status': 'PENDING', 'result': None}
        
        def run_task():
            self.tasks[task_id]['status'] = 'RUNNING'
            try:
                result = func(*args, **kwargs)
                self.tasks[task_id]['status'] = 'SUCCESS'
                self.tasks[task_id]['result'] = result
            except Exception as e:
                self.tasks[task_id]['status'] = 'FAILURE'
                self.tasks[task_id]['result'] = str(e)

        thread = threading.Thread(target=run_task)
        thread.start()
        return task_id

    def get_task_status(self, task_id):
        return self.tasks.get(task_id, {'status': 'NOT_FOUND'})

task_queue = SimpleTaskQueue()

@app.after_request
def debug_cors_headers(response):
    """Debug middleware to log CORS headers and cookies"""
    # logger.debug("\n=== Request Debug Info ===")
    # logger.debug(f"Request URL: {request.url}")
    # logger.debug(f"Request Endpoint: {request.endpoint}")
    # logger.debug(f"Request Origin: {request.headers.get('Origin')}")
    # logger.debug(f"Request Method: {request.method}")
    # logger.debug("\nRequest Headers:")
    # for header, value in request.headers.items():
        # logger.debug(f"{header}: {value}")
    
    # logger.debug("\nRequest Cookies:")
    # logger.debug(request.cookies)
    
    # logger.debug("\n=== Response Debug Info ===")
    # logger.debug("Response Headers:")
    # for header, value in response.headers.items():
        # logger.debug(f"{header}: {value}")
    
    # logger.debug("\nResponse Cookies:")
    # if 'Set-Cookie' in response.headers:
        # logger.debug(response.headers.getlist('Set-Cookie'))
    # else:
        # logger.debug("No cookies set in response")
    
    return response