from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import logging
import time

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
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

class CORSHTTPRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Get the origin from the request
        origin = self.headers.get('Origin')
        host = self.headers.get('Host')
        
        # Log request details
        logger.debug("\n=== Request Debug Info ===")
        logger.debug(f"Request Path: {self.path}")
        logger.debug(f"Request Origin: {origin}")
        logger.debug(f"Request Host: {host}")
        logger.debug(f"Request Protocol: {'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'}")
        logger.debug("\nRequest Headers:")
        for header, value in self.headers.items():
            logger.debug(f"  {header}: {value}")
        
        # Set CORS headers for any origin
        if origin:
            # Check if origin is from a known domain
            known_domains = ['godver3.xyz']
            origin_domain = origin.split('://')[-1].split(':')[0]
            is_known_domain = any(domain in origin_domain for domain in known_domains)
            
            logger.debug(f"Origin domain: {origin_domain}")
            logger.debug(f"Is known domain: {is_known_domain}")
            
            if is_known_domain:
                logger.debug(f"Setting CORS headers for known domain: {origin}")
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS, PUT, DELETE')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept, Accept-Language, Content-Language, Range, X-Requested-With, Cookie, X-CSRF-Token, Upgrade-Insecure-Requests')
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Expose-Headers', 'Set-Cookie')
                self.send_header('Vary', 'Origin')
            else:
                logger.debug(f"Unknown origin domain, restricting CORS headers")
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        
        super().end_headers()
    
    def do_GET(self):
        # Handle cookie check endpoint
        if self.path == '/cookie-check':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            
            # Get cookies from request
            cookies = self.headers.get('Cookie', '')
            cookie_list = []
            if cookies:
                for cookie in cookies.split(';'):
                    name, value = cookie.strip().split('=', 1)
                    cookie_list.append({
                        'name': name,
                        'value': value[:20] + '...' if len(value) > 20 else value,
                        'secure': 'Secure' in self.headers.get('Cookie-Flags', ''),
                        'httpOnly': 'HttpOnly' in self.headers.get('Cookie-Flags', ''),
                        'sameSite': 'SameSite' in self.headers.get('Cookie-Flags', '')
                    })
            
            # Log cookie information
            logger.debug("\n=== Cookie Check ===")
            logger.debug(f"Cookies received: {len(cookie_list)}")
            for cookie in cookie_list:
                logger.debug(f"  {cookie['name']}: {cookie['value']}")
                logger.debug(f"    Secure: {cookie['secure']}")
                logger.debug(f"    HttpOnly: {cookie['httpOnly']}")
                logger.debug(f"    SameSite: {cookie['sameSite']}")
            
            # Get protocol information
            protocol = 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
            
            # Send response with enhanced information
            import json
            response = {
                'cookies_present': bool(cookie_list),
                'cookie_count': len(cookie_list),
                'cookies': cookie_list,
                'request': {
                    'host': self.headers.get('Host', ''),
                    'origin': self.headers.get('Origin', ''),
                    'protocol': protocol,
                    'user_agent': self.headers.get('User-Agent', ''),
                    'x_forwarded_proto': self.headers.get('X-Forwarded-Proto', ''),
                    'x_forwarded_for': self.headers.get('X-Forwarded-For', '')
                },
                'server': {
                    'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'known_domains': ['godver3.xyz'],
                }
            }
            response_bytes = json.dumps(response, indent=2).encode('utf-8')
            
            self.send_header('Content-Length', len(response_bytes))
            self.end_headers()
            self.wfile.write(response_bytes)
            return
            
        # Handle favicon.ico requests
        elif self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
            
        return super().do_GET()
        
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

def run_server(port=8087, host='0.0.0.0'):
    server_address = (host, port)
    httpd = HTTPServer(server_address, CORSHTTPRequestHandler)
    logger.info(f"Starting CORS test server on {host}:{port}...")
    logger.info(f"Access the test page using the same base domain as your main application")
    logger.info(f"Example URLs to test:")
    logger.info(f"  - https://cli-debrid.godver3.xyz")
    logger.info(f"  - http://cli-test.godver3.xyz")
    logger.info(f"Make sure to test both HTTP and HTTPS combinations")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server() 