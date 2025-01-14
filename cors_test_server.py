from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import logging

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CORSHTTPRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Get the origin from the request
        origin = self.headers.get('Origin')
        
        # Set CORS headers for any origin
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept, X-Requested-With, Cookie')
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Access-Control-Expose-Headers', 'Set-Cookie')
            
        super().end_headers()
    
    def do_GET(self):
        # Handle favicon.ico requests
        if self.path == '/favicon.ico':
            self.send_response(204)  # No content
            self.end_headers()
            return
            
        return super().do_GET()
        
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

def run_server(port=8087):
    server_address = ('', port)
    httpd = HTTPServer(server_address, CORSHTTPRequestHandler)
    logger.info(f"Starting CORS test server on port {port}...")
    logger.info(f"Access the test page at: http://192.168.1.51:{port}/cors_test.html")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server() 