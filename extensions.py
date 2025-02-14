from flask import Flask, redirect, request, jsonify, url_for, session, g
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
from tld import get_tld
from tld.exceptions import TldDomainNotFound, TldBadUrl
from werkzeug.exceptions import HTTPException

# Configure logging at INFO level only
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(request_id)s] - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, 'request_id', 'no_request_id')
        return True

logger.addFilter(RequestIDFilter())

class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.wrapped_app = app

    def __call__(self, environ, start_response):
        def security_headers_start_response(status, headers, exc_info=None):
            security_headers = [
                ('X-Content-Type-Options', 'nosniff'),
                ('X-Frame-Options', 'SAMEORIGIN'),
                ('X-XSS-Protection', '1; mode=block'),
                ('Referrer-Policy', 'strict-origin-when-cross-origin'),
                ('Permissions-Policy', 'geolocation=(), microphone=(), camera=()'),
                ('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline';")
            ]
            headers.extend(security_headers)
            return start_response(status, headers, exc_info)
        return self.wrapped_app(environ, security_headers_start_response)

class RequestIDMiddleware:
    def __init__(self, app):
        self.flask_app = app
        self.wrapped_app = app.wsgi_app if isinstance(app, Flask) else app

    def __call__(self, environ, start_response):
        with self.flask_app.request_context(environ):
            g.request_id = str(uuid.uuid4())
            def request_id_start_response(status, headers, exc_info=None):
                headers.append(('X-Request-ID', g.request_id))
                return start_response(status, headers, exc_info)
            return self.wrapped_app(environ, request_id_start_response)

MAX_REDIRECTS = 10
REDIRECT_TIMEOUT = 30  # seconds

def is_redirect_loop(response):
    """Check if we're in a redirect loop based on session data"""
    # Check if status code indicates a redirect (3xx)
    is_redirect = 300 <= int(response.status.split()[0]) < 400
    
    if not is_redirect:
        # Reset redirect tracking on non-redirect responses
        session.pop('redirect_count', None)
        session.pop('first_redirect_time', None)
        return False
        
    current_time = time.time()
    redirect_count = session.get('redirect_count', 0)
    first_redirect_time = session.get('first_redirect_time')
    
    if not first_redirect_time:
        session['first_redirect_time'] = current_time
        session['redirect_count'] = 1
        return False
        
    # Check if we should reset the counter due to timeout
    if current_time - first_redirect_time > REDIRECT_TIMEOUT:
        session['first_redirect_time'] = current_time
        session['redirect_count'] = 1
        return False
        
    # Increment redirect count
    redirect_count += 1
    session['redirect_count'] = redirect_count
    
    # Check if we've hit the maximum redirects
    if redirect_count >= MAX_REDIRECTS:
        session.pop('redirect_count', None)
        session.pop('first_redirect_time', None)
        return True
        
    return False

class RedirectLoopProtection:
    def __init__(self, app):
        self.flask_app = app
        self.wrapped_app = app.wsgi_app if isinstance(app, Flask) else app
        
    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            try:
                with self.flask_app.request_context(environ):
                    response = self.flask_app.response_class()
                    response.status = status
                    response.headers = headers
                    
                    if is_redirect_loop(response):
                        logging.error(f"Detected redirect loop at: {request.path}")
                        # Return a 508 Loop Detected error
                        error_response = jsonify({
                            'error': 'Redirect Loop Detected',
                            'message': 'The server detected an infinite redirect loop',
                            'path': request.path
                        })
                        error_response.status_code = 508
                        return error_response(environ, start_response)
                        
            except Exception as e:
                logging.error(f"Error in redirect loop detection: {str(e)}")
                
            return start_response(status, headers, exc_info)
            
        return self.wrapped_app(environ, custom_start_response)

def get_root_domain(host):
    """Get the root domain from a hostname."""
    if not host:
        return None
        
    # Remove port if present and ensure no leading/trailing dots
    domain = host.split(':')[0].lower().strip('.')
    
    # If localhost or IP, return as is
    if domain in ('localhost', '127.0.0.1', '::1') or domain.replace('.', '').isdigit():
        return domain
    
    try:
        # Try to get the registered domain using tld library
        # This will properly handle all domain structures including multi-level TLDs
        res = get_tld(f"http://{domain}", as_object=True)
        # For subdomains, return the full domain to ensure cookies work across all subdomains
        result_domain = domain if res.subdomain else res.fld
        # Ensure no leading/trailing dots in the final result
        return result_domain.strip('.')
    except (TldDomainNotFound, TldBadUrl):
        # If domain parsing fails, return the full domain to be safe
        return domain if '.' in domain else None

class SameSiteMiddleware:
    def __init__(self, app):
        self.wrapped_app = app

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            new_headers = []
            host = environ.get('HTTP_HOST', '')
            root_domain = get_root_domain(host)
            proto = environ.get('HTTP_X_FORWARDED_PROTO', environ.get('wsgi.url_scheme', 'http'))
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
                        is_clearing = ('=' not in cookie_main or 
                                     cookie_main.split('=')[1] == '' or 
                                     cookie_main.endswith('='))
                        
                        if is_clearing:
                            value = f"{cookie_main}; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
                            if root_domain:
                                value += f"; Domain={root_domain}"
                        else:
                            cookie_attrs['samesite'] = 'SameSite=Lax'
                            if is_secure:
                                cookie_attrs['secure'] = 'Secure'
                            
                            if root_domain:
                                cookie_attrs['domain'] = f'Domain={root_domain}'
                            cookie_attrs['path'] = 'Path=/'
                            
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
        
        return self.wrapped_app(environ, custom_start_response)

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
app = Flask(__name__)

# Create middleware chain in correct order
# Each middleware wraps the previous one
base_wsgi_app = app.wsgi_app
base_wsgi_app = ProxyFix(base_wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
base_wsgi_app = SecurityHeadersMiddleware(base_wsgi_app)
base_wsgi_app = RedirectLoopProtection(app)  # Needs Flask app for request context
base_wsgi_app = SameSiteMiddleware(base_wsgi_app)
base_wsgi_app = RequestIDMiddleware(app)  # Needs Flask app for request context

# Set the final middleware chain
app.wsgi_app = base_wsgi_app

# Configure session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
app.config['SESSION_FILE_DIR'] = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'flask_session')
app.config['SESSION_FILE_THRESHOLD'] = 500
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = False
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'session'
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_DOMAIN'] = None  # Let Flask determine the domain without leading period

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
    "origins": ["http://*", "https://*"],
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
    login_manager.session_protection = 'strong'
    
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
    pass

@app.before_request
def check_user_system():
    if request.path.startswith('/webhook') or request.path.startswith('/static') or request.path.startswith('/debug'):
        return

@app.before_request
def configure_session():
    proto = request.headers.get('X-Forwarded-Proto', 'http')
    scheme = request.headers.get('X-Forwarded-Scheme', proto)
    root_domain = get_root_domain(request.host)
    
    if root_domain:
        app.config['SESSION_COOKIE_DOMAIN'] = root_domain.strip('.')
    else:
        app.config['SESSION_COOKIE_DOMAIN'] = None
    
    is_secure = (scheme == 'https')
    app.config['SESSION_COOKIE_SECURE'] = is_secure
    app.config['REMEMBER_COOKIE_SECURE'] = is_secure
    
    if is_secure:
        app.config['SESSION_COOKIE_SAMESITE'] = 'None'
        app.config['REMEMBER_COOKIE_SAMESITE'] = 'None'
    else:
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
        app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    origin = request.headers.get('Origin')
    if origin:
        if origin.endswith(request.host):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
            response.headers['Vary'] = 'Origin'
        else:
            origin_domain = get_root_domain(origin.split('://')[-1])
            root_domain = get_root_domain(request.host)
            if root_domain and origin_domain and root_domain == origin_domain:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Credentials'] = 'true'
                response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
                response.headers['Vary'] = 'Origin'
    
    if request.method == 'OPTIONS':
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, POST, OPTIONS, PUT, DELETE'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, Accept, Origin, Cookie'
        response.headers['Access-Control-Max-Age'] = '3600'
    
    return response

@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request", "message": str(error)}), 400

@app.route('/scraper/static/images/placeholder.png')
def redirect_placeholder():
    return redirect('/static/images/placeholder.png')

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

def init_error_handlers(app):
    @app.errorhandler(Exception)
    def handle_exception(e):
        # Log the error with request ID
        logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
        
        # Pass through HTTP errors
        if isinstance(e, HTTPException):
            return e

        # Return generic error response
        return jsonify({
            'error': 'Internal Server Error',
            'request_id': getattr(g, 'request_id', None),
            'message': 'An unexpected error occurred'
        }), 500

    @app.errorhandler(429)
    def handle_rate_limit(e):
        return jsonify({
            'error': 'Too Many Requests',
            'request_id': getattr(g, 'request_id', None),
            'message': 'Rate limit exceeded'
        }), 429

def cleanup_expired_sessions():
    """Cleanup expired sessions periodically"""
    session_dir = app.config['SESSION_FILE_DIR']
    if os.path.exists(session_dir):
        current_time = time.time()
        for filename in os.listdir(session_dir):
            filepath = os.path.join(session_dir, filename)
            try:
                if os.path.getctime(filepath) + app.config['PERMANENT_SESSION_LIFETIME'].total_seconds() < current_time:
                    os.remove(filepath)
            except OSError:
                pass

# Initialize error handlers
init_error_handlers(app)

# Schedule session cleanup
cleanup_thread = threading.Thread(target=cleanup_expired_sessions, daemon=True)
cleanup_thread.start()