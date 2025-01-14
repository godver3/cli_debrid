from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import logging

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
        logger.debug("\nRequest Headers:")
        for header, value in self.headers.items():
            logger.debug(f"  {header}: {value}")
        
        # Set CORS headers for any origin
        if origin:
            logger.debug(f"Setting CORS headers for origin: {origin}")
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept, Accept-Language, Content-Language, Range, X-Requested-With, Cookie, X-CSRF-Token, Upgrade-Insecure-Requests')
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Access-Control-Expose-Headers', 'Set-Cookie')
            self.send_header('Vary', 'Origin')
        
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
                        'value': value[:20] + '...' if len(value) > 20 else value
                    })
            
            # Log cookie information
            logger.debug("\n=== Cookie Check ===")
            logger.debug(f"Cookies received: {len(cookie_list)}")
            for cookie in cookie_list:
                logger.debug(f"  {cookie['name']}: {cookie['value']}")
            
            # Send response with cookie information
            import json
            response = {
                'cookies_present': bool(cookie_list),
                'cookie_count': len(cookie_list),
                'cookies': cookie_list,
                'host': self.headers.get('Host', ''),
                'origin': self.headers.get('Origin', ''),
                'user_agent': self.headers.get('User-Agent', '')
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
    logger.info(f"Example: if your app is at cli-debrid.example.com, access this at cors-test.example.com:{port}")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server() 