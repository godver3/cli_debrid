from flask import Flask, redirect, request, jsonify, url_for, session
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
    # For hostnames, get root domain without leading dot
    parts = domain.split('.')
    if len(parts) > 2:
        return '.'.join(parts[-2:])  # e.g., example.com for sub.example.com
    return domain  # e.g., localhost

class SameSiteMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            new_headers = []
            host = environ.get('HTTP_HOST', '')
            root_domain = get_root_domain(host)
            proto = environ.get('HTTP_X_FORWARDED_PROTO', 'http')
            is_secure = (proto == 'https')
            
            for name, value in headers:
                if name.lower() == 'set-cookie':
                    # Parse the cookie
                    parts = [p.strip() for p in value.split(';')]
                    cookie_main = parts[0]
                    cookie_attrs = {
                        p.split('=')[0].lower(): p for p in parts[1:]
                        if '=' in p or p.lower() in ['secure', 'httponly']
                    }
                    
                    # Handle session and remember token cookies
                    if 'session=' in cookie_main or 'remember_token=' in cookie_main:
                        # Check if this is a cookie clearing operation (empty or deleted value)
                        is_clearing = ('=' not in cookie_main or 
                                     cookie_main.split('=')[1] == '' or 
                                     cookie_main.endswith('='))
                        
                        if is_clearing:
                            # For cookie clearing, set expires in the past and clear the value
                            value = f"{cookie_main}; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
                            if root_domain:
                                value += f"; Domain={root_domain}"
                        else:
                            # Normal cookie setting
                            if is_secure:
                                cookie_attrs['samesite'] = 'SameSite=None'
                                cookie_attrs['secure'] = 'Secure'
                            else:
                                cookie_attrs['samesite'] = 'SameSite=Lax'
                            
                            if root_domain:
                                cookie_attrs['domain'] = f'Domain={root_domain}'
                            cookie_attrs['path'] = 'Path=/'
                            
                            # Reconstruct the cookie with attributes in specific order
                            value = '; '.join([
                                cookie_main,
                                cookie_attrs.get('path', 'Path=/'),
                                cookie_attrs.get('domain', ''),
                                cookie_attrs.get('samesite', 'SameSite=Lax'),
                                cookie_attrs.get('secure', ''),
                                cookie_attrs.get('httponly', 'HttpOnly')
                            ]).rstrip('; ')
                    
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
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
app.config['SESSION_FILE_DIR'] = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'flask_session')
app.config['SESSION_FILE_THRESHOLD'] = 500
app.config['SESSION_COOKIE_SECURE'] = False  # Allow both HTTP and HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = False  # Allow both HTTP and HTTPS
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'session'
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['SESSION_COOKIE_PATH'] = '/'

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
    "origins": ["http://*", "https://*"],  # Allow both HTTP and HTTPS origins
    "methods": ["GET", "HEAD", "POST", "OPTIONS", "PUT", "DELETE"],
    "allow_headers": ["Content-Type", "Authorization", "X-Requested-With", "Accept", "Origin", "Cookie"],
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
    # HTTPS redirect disabled
    pass

@app.before_request
def check_user_system():
    # Exclude the webhook route, its subpaths, and static files
    if request.path.startswith('/webhook') or request.path.startswith('/static') or request.path.startswith('/debug'):
        return    

    # Remove any specific handling for root.root here
    # The decorators will handle the logic now

@app.before_request
def configure_session():
    """Configure session settings before each request"""
    proto = request.headers.get('X-Forwarded-Proto', 'http')
    scheme = request.headers.get('X-Forwarded-Scheme', proto)
    root_domain = get_root_domain(request.host)
    
    if root_domain:
        app.config['SESSION_COOKIE_DOMAIN'] = root_domain
        app.config['REMEMBER_COOKIE_DOMAIN'] = root_domain
    else:
        # For localhost/IP, don't set domain
        app.config['SESSION_COOKIE_DOMAIN'] = None
        app.config['REMEMBER_COOKIE_DOMAIN'] = None
    
    # Set cookie security based on protocol
    is_secure = (scheme == 'https')
    app.config['SESSION_COOKIE_SECURE'] = is_secure
    app.config['REMEMBER_COOKIE_SECURE'] = is_secure
    
    # Set SameSite attribute based on protocol
    if is_secure:
        app.config['SESSION_COOKIE_SAMESITE'] = 'None'  # Must be 'None' for cross-site HTTPS
        app.config['REMEMBER_COOKIE_SAMESITE'] = 'None'
    else:
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Default to Lax for HTTP
        app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

@app.after_request
def add_security_headers(response):
    """Add security headers and handle cookies"""
    logging.debug("[login_testing] Processing request for URL: %s", request.url)
    logging.debug("[login_testing] Request cookies: %s", dict(request.cookies))
    logging.debug("[login_testing] Request headers: %s", dict(request.headers))
    
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    # Handle CORS
    origin = request.headers.get('Origin')
    if origin:
        # Allow same-origin requests always
        if origin.endswith(request.host):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
            response.headers['Vary'] = 'Origin'
            logging.debug("[login_testing] Setting CORS headers for same origin: %s", origin)
        else:
            # For cross-origin, verify domain
            origin_domain = get_root_domain(origin.split('://')[-1])
            root_domain = get_root_domain(request.host)
            if root_domain and origin_domain and root_domain == origin_domain:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Credentials'] = 'true'
                response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
                response.headers['Vary'] = 'Origin'
                logging.debug("[login_testing] Setting CORS headers for origin: %s", origin)
    
    # For OPTIONS requests, add necessary CORS headers
    if request.method == 'OPTIONS':
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, POST, OPTIONS, PUT, DELETE'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, Accept, Origin, Cookie'
        response.headers['Access-Control-Max-Age'] = '3600'
    
    # Log final headers and cookies
    logging.debug("[login_testing] Final response headers: %s", dict(response.headers))
    logging.debug("[login_testing] Final cookies: %s", response.headers.getlist('Set-Cookie'))
    
    # Handle login and logout responses
    if request.endpoint:
        if (request.endpoint == 'auth.login' and 
            response.status_code == 302 and 
            'session' in session):
            logging.debug("[login_testing] Login redirect detected, ensuring session cookie is set")
            # Force the session cookie to be set in the response
            if not response.headers.getlist('Set-Cookie'):
                session.modified = True
                app.session_interface.save_session(app, session, response)
                logging.debug("[login_testing] Manually set session cookie: %s", response.headers.getlist('Set-Cookie'))
        elif request.endpoint == 'auth.logout':
            logging.debug("[login_testing] Logout detected, ensuring cookies are cleared")
            response.set_cookie('session', '', expires=0, path='/', domain=app.config['SESSION_COOKIE_DOMAIN'])
            response.set_cookie('remember_token', '', expires=0, path='/', domain=app.config['REMEMBER_COOKIE_DOMAIN'])
    
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