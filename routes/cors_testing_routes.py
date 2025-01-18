from flask import Blueprint, jsonify, request, render_template, current_app
import logging
import time
from flask_login import current_user

cors_testing_bp = Blueprint('cors_testing', __name__, url_prefix='/cors_testing')

@cors_testing_bp.route('/')
def cors_test_page():
    """Serve the CORS test page"""
    return render_template('cors_test.html')

@cors_testing_bp.route('/cookie-check')
def cookie_check():
    """Check cookies and request details"""
    cookies = request.cookies
    cookie_list = []
    
    for name, value in cookies.items():
        cookie_list.append({
            'name': name,
            'value': value[:20] + '...' if len(value) > 20 else value,
            'secure': current_app.config['SESSION_COOKIE_SECURE'] if name == 'session' else None,
            'httpOnly': current_app.config['SESSION_COOKIE_HTTPONLY'] if name == 'session' else None,
            'sameSite': current_app.config['SESSION_COOKIE_SAMESITE'] if name == 'session' else None
        })
    
    # Log cookie information
    logging.debug("\n=== Cookie Check ===")
    logging.debug(f"Cookies received: {len(cookie_list)}")
    for cookie in cookie_list:
        logging.debug(f"  {cookie['name']}: {cookie['value']}")
        logging.debug(f"    Secure: {cookie['secure']}")
        logging.debug(f"    HttpOnly: {cookie['httpOnly']}")
        logging.debug(f"    SameSite: {cookie['sameSite']}")
    
    # Get protocol information
    protocol = request.headers.get('X-Forwarded-Proto', request.scheme)
    
    # Send response with enhanced information
    response = {
        'cookies_present': bool(cookie_list),
        'cookie_count': len(cookie_list),
        'cookies': cookie_list,
        'request': {
            'host': request.host,
            'origin': request.headers.get('Origin'),
            'protocol': protocol,
            'user_agent': request.headers.get('User-Agent'),
            'x_forwarded_proto': request.headers.get('X-Forwarded-Proto'),
            'x_forwarded_for': request.headers.get('X-Forwarded-For')
        },
        'server': {
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'session_config': {
                'cookie_secure': current_app.config['SESSION_COOKIE_SECURE'],
                'cookie_httponly': current_app.config['SESSION_COOKIE_HTTPONLY'],
                'cookie_samesite': current_app.config['SESSION_COOKIE_SAMESITE'],
                'cookie_domain': current_app.config.get('SESSION_COOKIE_DOMAIN'),
                'permanent': current_app.config['SESSION_PERMANENT']
            },
            'user_authenticated': current_user.is_authenticated if not current_user.is_anonymous else False
        }
    }
    
    return jsonify(response)

@cors_testing_bp.route('/test-post', methods=['POST'])
def test_post():
    """Test POST request handling"""
    response = {
        'status': 'success',
        'method': request.method,
        'headers': dict(request.headers),
        'form_data': dict(request.form),
        'user_authenticated': current_user.is_authenticated if not current_user.is_anonymous else False
    }
    return jsonify(response)

@cors_testing_bp.route('/test-options', methods=['OPTIONS'])
def test_options():
    """Test OPTIONS request handling"""
    response = jsonify({
        'status': 'success',
        'allowed_methods': ['GET', 'POST', 'OPTIONS'],
        'cors_enabled': True
    })
    return response 