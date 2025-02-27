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
from tld import get_tld
from tld.exceptions import TldDomainNotFound, TldBadUrl

# Configure logging at INFO level only
#logging.basicConfig(level=logging.INFO)
#logger = logging.getLogger(__name__)

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
    except Exception as e:
        # Handle TldIOError and other exceptions
        import logging
        logging.warning(f"Error in get_root_domain: {str(e)}")
        
        # Fallback to basic domain parsing
        parts = domain.split('.')
        if len(parts) > 1:
            # Simple heuristic: for domains with 2 parts, return as is
            # For domains with 3+ parts, return last 2 parts if the TLD is common
            # Otherwise return the full domain
            if len(parts) == 2:
                return domain
            elif parts[-1].lower() in ('com', 'org', 'net', 'edu', 'gov', 'io', 'co'):
                if len(parts) == 3 and parts[-2].lower() in ('co', 'com', 'org', 'net'):
                    # Handle special cases like .co.uk
                    return '.'.join(parts[-3:])
                return '.'.join(parts[-2:])
            else:
                return domain
        return domain

class SameSiteMiddleware:
    def __init__(self, app):
        self.app = app

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
        
        return self.app(environ, custom_start_response)

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
app = Flask(__name__)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.wsgi_app = SameSiteMiddleware(app.wsgi_app)

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