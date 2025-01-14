from flask_sqlalchemy import SQLAlchemy
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

db = SQLAlchemy()
app = Flask(__name__)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configure session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
app.secret_key = os.urandom(24)  # Generate a secure secret key

# Configure session cookie settings
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # More compatible than None
app.config['SESSION_COOKIE_SECURE'] = True  # Require HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent XSS
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_DOMAIN'] = None  # Will be set dynamically

# Configure CORS with specific origins
CORS(app, resources={r"/*": {
    "origins": ["*"],
    "methods": ["GET", "HEAD", "POST", "OPTIONS", "PUT", "DELETE"],
    "allow_headers": ["Content-Type", "Authorization", "Accept", "Accept-Language", 
                     "Content-Language", "Range", "X-Requested-With", "Cookie", 
                     "X-CSRF-Token", "Upgrade-Insecure-Requests"],
    "supports_credentials": True,
    "expose_headers": ["Set-Cookie", "Content-Range"],
    "max_age": 3600
}})

from flask_login import LoginManager
from flask import redirect, url_for
from functools import wraps

login_manager = LoginManager()

def init_login_manager(app):
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

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
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    if origin:  # Only add CORS headers if there's an Origin header
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Accept, Accept-Language, Content-Language, Range, X-Requested-With, Cookie'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
        
        if request.method == 'OPTIONS':
            response.headers['Access-Control-Max-Age'] = '3600'
    
    return response

@app.after_request
def add_security_headers(response):
    # Remove the upgrade-insecure-requests directive
    # response.headers['Content-Security-Policy'] = "upgrade-insecure-requests"
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
def after_request(response):
    # Get the origin from the request
    origin = request.headers.get('Origin')
    forwarded_proto = request.headers.get('X-Forwarded-Proto', 'http')
    
    # Always set security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    
    # Handle cookies
    if 'Set-Cookie' in response.headers or response.status_code in [301, 302]:
        cookies = response.headers.getlist('Set-Cookie')
        response.headers.remove('Set-Cookie')
        
        # Get the domain from the request host
        host = request.host.split(':')[0]  # Remove port if present
        domain_parts = host.split('.')
        if len(domain_parts) > 2:
            domain = '.' + '.'.join(domain_parts[-2:])  # e.g., .godver3.xyz
        else:
            domain = '.' + host  # e.g., .localhost
            
        #logger.debug(f"Setting cookie domain to: {domain}")
        
        # If no cookies but we're redirecting, ensure session cookie is set
        if not cookies and response.status_code in [301, 302]:
            session_cookie = f"session={request.cookies.get('session', '')}; Path=/; Domain={domain}; Secure; SameSite=Lax"
            cookies.append(session_cookie)
        
        for cookie in cookies:
            if 'SameSite=' not in cookie:
                cookie += '; SameSite=Lax'
            if 'Domain=' not in cookie:
                cookie += f'; Domain={domain}'
            if 'Secure' not in cookie:
                cookie += '; Secure'
            response.headers.add('Set-Cookie', cookie)
            #logger.debug(f"Modified cookie: {cookie}")
    
    # Set CORS headers for all requests
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Expose-Headers'] = 'Set-Cookie'
        #logger.debug(f"Set CORS headers for origin: {origin}")
    
    return response

@app.after_request
def debug_cors_headers(response):
    """Debug middleware to log CORS headers and cookies"""
    #logger.debug("\n=== Request Debug Info ===")
    #logger.debug(f"Request URL: {request.url}")
    #logger.debug(f"Request Origin: {request.headers.get('Origin')}")
    #logger.debug(f"Request Method: {request.method}")
    #logger.debug("\nRequest Headers:")
    #for header, value in request.headers.items():
    #    logger.debug(f"{header}: {value}")
    
    #logger.debug("\nRequest Cookies:")
    #logger.debug(request.cookies)
    
    #logger.debug("\n=== Response Debug Info ===")
    #logger.debug("Response Headers:")
    #for header, value in response.headers.items():
    #    logger.debug(f"{header}: {value}")
    
    #logger.debug("\nResponse Cookies:")
    #if 'Set-Cookie' in response.headers:
    #    logger.debug(response.headers.getlist('Set-Cookie'))
    #else:
    #    logger.debug("No cookies set in response")
    
    return response