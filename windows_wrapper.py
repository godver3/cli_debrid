import os
import sys
import multiprocessing
import logging
import traceback
import runpy
import time
import appdirs
import socket
import select
import threading
import psutil  # For memory and process monitoring
import gc      # For garbage collection monitoring
import datetime
import subprocess  # For running system commands
from utilities.settings import get_setting  # Import the proper get_setting function
import platform
import requests
import tempfile

# Default ports configuration
DEFAULT_PORTS = {
    'win32': {
        'main': 40000,
        'battery': 40001,
        'tunnel_main': 40000,
        'tunnel_battery': 40001
    },
    'default': {
        'main': 5000,
        'battery': 5001,
        'tunnel_main': 5000,
        'tunnel_battery': 5001
    }
}

# Set up logging to write to a file
log_dir = os.path.join(
    os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(),
    'logs'
)
os.makedirs(log_dir, exist_ok=True)

# Use timestamp in log filename to avoid overwriting previous logs
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(log_dir, f'cli_debrid_{timestamp}.log')

logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# Also print to console
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# Create a logger for resource monitoring
resource_logger = logging.getLogger('resource_monitor')

# Track active threads and connections
active_threads = {}
active_connections = {}

def log_system_resources():
    """Log current system resource usage"""
    try:
        process = psutil.Process()
        mem_info = process.memory_info()
        
        resource_logger.info(f"Memory usage: {mem_info.rss / (1024 * 1024):.2f} MB")
        resource_logger.info(f"CPU usage: {process.cpu_percent(interval=0.1)}%")
        resource_logger.info(f"Open files: {len(process.open_files())}")
        resource_logger.info(f"Active threads: {threading.active_count()}")
        resource_logger.info(f"Active connections: {len(active_connections)}")
        
        # Log garbage collector stats
        gc_counts = gc.get_count()
        resource_logger.info(f"GC counts: {gc_counts}")
        
        # Log thread names
        thread_names = [t.name for t in threading.enumerate()]
        resource_logger.debug(f"Thread names: {thread_names}")
    except Exception as e:
        resource_logger.error(f"Error logging system resources: {str(e)}")

# Start periodic resource logging
def start_resource_monitoring(interval=60):
    """Start a thread to periodically log resource usage"""
    def monitor_resources():
        while True:
            log_system_resources()
            time.sleep(interval)
    
    monitor_thread = threading.Thread(target=monitor_resources, name="ResourceMonitor")
    monitor_thread.daemon = True
    monitor_thread.start()
    return monitor_thread

def setup_environment():
    # Set default ports based on platform
    ports = DEFAULT_PORTS.get(sys.platform, DEFAULT_PORTS['default'])
    if 'CLI_DEBRID_PORT' not in os.environ:
        os.environ['CLI_DEBRID_PORT'] = str(ports['main'])
    if 'CLI_DEBRID_BATTERY_PORT' not in os.environ:
        os.environ['CLI_DEBRID_BATTERY_PORT'] = str(ports['battery'])
    if 'CLI_DEBRID_TUNNEL_PORT' not in os.environ:
        os.environ['CLI_DEBRID_TUNNEL_PORT'] = str(ports['tunnel_main'])
    if 'CLI_DEBRID_BATTERY_TUNNEL_PORT' not in os.environ:
        os.environ['CLI_DEBRID_BATTERY_TUNNEL_PORT'] = str(ports['tunnel_battery'])

    if sys.platform.startswith('win'):
        app_name = "cli_debrid"
        app_author = "cli_debrid"
        base_path = appdirs.user_data_dir(app_name, app_author)
        config_dir = os.path.join(base_path, 'config')
        logs_dir = os.path.join(base_path, 'logs')
        db_content_dir = os.path.join(base_path, 'db_content')
        
        os.environ['USER_CONFIG'] = config_dir
        os.environ['USER_LOGS'] = logs_dir
        os.environ['USER_DB_CONTENT'] = db_content_dir
    else:
        os.environ.setdefault('USER_CONFIG', '/user/config')
        os.environ.setdefault('USER_LOGS', '/user/logs')
        os.environ.setdefault('USER_DB_CONTENT', '/user/db_content')

    # Ensure directories exist
    for dir_path in [os.environ['USER_CONFIG'], os.environ['USER_LOGS'], os.environ['USER_DB_CONTENT']]:
        os.makedirs(dir_path, exist_ok=True)
        
    # Create empty lock file if it doesn't exist
    lock_file = os.path.join(os.environ['USER_CONFIG'], '.config.lock')
    if not os.path.exists(lock_file):
        open(lock_file, 'w').close()

def adjust_sys_path():
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Add base directory to sys.path
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    
    # Add cli_battery directory to sys.path
    cli_battery_dir = os.path.join(base_dir, 'cli_battery')
    if cli_battery_dir not in sys.path:
        sys.path.insert(0, cli_battery_dir)

def get_script_path(script_name):
    if getattr(sys, 'frozen', False):
        # When frozen, look for scripts in the same directory as the executable
        base_dir = os.path.dirname(sys.executable)
        # Try to find the script in the _MEIPASS directory first (PyInstaller temp directory)
        if hasattr(sys, '_MEIPASS'):
            meipass_path = os.path.join(sys._MEIPASS, script_name)
            if os.path.exists(meipass_path):
                return meipass_path
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, script_name)

def is_port_available(port):
    """Check if a port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False

def find_available_port(start_port):
    """Find the next available port starting from start_port."""
    port = start_port
    while not is_port_available(port):
        port += 1
        if port > 65535:
            raise RuntimeError("No available ports found")
    return port

def is_service_ready(port, max_attempts=30, delay=0.5):
    """Check if a service is listening on the specified port."""
    for _ in range(max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(('127.0.0.1', port))
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(delay)
    return False

def create_tunnel(remote_port, local_port, buffer_size=4096):
    """Creates a tunnel from a remote port to a local port."""
    def handle_client(client_sock, local_port, client_id):
        thread_name = threading.current_thread().name
        active_threads[thread_name] = time.time()
        active_connections[client_id] = {
            'start_time': time.time(),
            'bytes_sent': 0,
            'bytes_received': 0,
            'last_activity': time.time()
        }
        
        try:
            # Connect to local service with retry
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            retry_count = 0
            max_retries = 5
            while retry_count < max_retries:
                try:
                    local_sock.connect(('127.0.0.1', local_port))
                    break
                except ConnectionRefusedError:
                    retry_count += 1
                    if retry_count == max_retries:
                        logging.error(f"Failed to connect to local service after {max_retries} attempts")
                        return
                    time.sleep(0.5)
            
            idle_timeout = 300  # 5 minutes
            last_activity = time.time()
            
            while True:
                # Check for idle timeout
                if time.time() - last_activity > idle_timeout:
                    logging.warning(f"Connection {client_id} timed out after {idle_timeout} seconds of inactivity")
                    break
                
                # Wait for data from either socket with timeout
                try:
                    readable, _, exceptional = select.select([client_sock, local_sock], [], [client_sock, local_sock], 60)
                    
                    if exceptional:
                        logging.warning(f"Exceptional condition on sockets for client {client_id}")
                        break
                    
                    if not readable:  # Timeout occurred
                        continue
                    
                    for sock in readable:
                        other_sock = local_sock if sock is client_sock else client_sock
                        try:
                            data = sock.recv(buffer_size)
                            if not data:
                                logging.debug(f"No data received from {'client' if sock is client_sock else 'local'} socket for client {client_id}")
                                return
                            
                            bytes_count = len(data)
                            if sock is client_sock:
                                active_connections[client_id]['bytes_received'] += bytes_count
                            else:
                                active_connections[client_id]['bytes_sent'] += bytes_count
                                
                            other_sock.sendall(data)
                            last_activity = time.time()
                            active_connections[client_id]['last_activity'] = last_activity
                        except Exception as e:
                            logging.error(f"Socket error for client {client_id}: {str(e)}")
                            return
                except Exception as e:
                    logging.error(f"Select error for client {client_id}: {str(e)}")
                    break
        except Exception as e:
            logging.error(f"Error in tunnel connection for client {client_id}: {str(e)}")
            logging.debug(f"Detailed error: {traceback.format_exc()}")
        finally:
            try:
                if client_id in active_connections:
                    conn_stats = active_connections.pop(client_id)
                    duration = time.time() - conn_stats['start_time']
                    logging.info(f"Connection {client_id} closed. Duration: {duration:.2f}s, Bytes sent: {conn_stats['bytes_sent']}, Bytes received: {conn_stats['bytes_received']}")
                
                if thread_name in active_threads:
                    del active_threads[thread_name]
                
                client_sock.close()
                local_sock.close()
            except Exception as e:
                logging.error(f"Error closing sockets for client {client_id}: {str(e)}")

    def tunnel_server():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Set socket timeout to prevent blocking indefinitely
        server.settimeout(300)  # 5 minute timeout
        
        client_counter = 0
        
        try:
            server.bind(('0.0.0.0', remote_port))
            server.listen(5)
            logging.info(f"Port forwarding active: {remote_port} -> 127.0.0.1:{local_port}")
            
            while True:
                try:
                    client_sock, addr = server.accept()
                    client_counter += 1
                    client_id = f"{addr[0]}:{addr[1]}_{client_counter}"
                    
                    logging.info(f"New connection from {addr[0]}:{addr[1]} (ID: {client_id})")
                    
                    client_thread = threading.Thread(
                        target=handle_client,
                        args=(client_sock, local_port, client_id),
                        name=f"Tunnel-{client_id}"
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just a timeout on accept, continue
                    logging.debug("Socket accept timeout, continuing...")
                    continue
                except Exception as e:
                    logging.error(f"Error accepting connection: {str(e)}")
                    logging.debug(f"Detailed error: {traceback.format_exc()}")
        except Exception as e:
            logging.error(f"Error in tunnel server: {str(e)}")
            logging.debug(f"Detailed error: {traceback.format_exc()}")
        finally:
            try:
                server.close()
            except:
                pass

    tunnel_thread = threading.Thread(target=tunnel_server, name=f"TunnelServer-{remote_port}")
    tunnel_thread.daemon = True
    tunnel_thread.start()
    return tunnel_thread

def run_script(script_name, port=None, battery_port=None, host=None):
    script_path = get_script_path(script_name)
    logging.info(f"Running script: {script_path}")
    try:
        # Add the directory containing the script to sys.path
        script_dir = os.path.dirname(script_path)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        
        # Set up environment before running the script
        setup_environment()
        
        # Set environment variables for ports and host
        if port:
            os.environ['CLI_DEBRID_PORT'] = str(port)
        if battery_port:
            os.environ['CLI_DEBRID_BATTERY_PORT'] = str(battery_port)
        if host:
            os.environ['CLI_DEBRID_HOST'] = host
            
        runpy.run_path(script_path, run_name='__main__')
    except Exception as e:
        logging.error(f"Error running script {script_name}: {str(e)}")
        logging.error(traceback.format_exc())

def check_npm_available():
    try:
        import subprocess
        result = subprocess.run(['npm', '--version'], 
                             shell=True,
                             capture_output=True,
                             text=True)
        return result.returncode == 0
    except Exception:
        return False

def install_nodejs():
    try:
        if platform.system() != 'Windows':
            logging.warning("Node.js automatic installation is only supported on Windows")
            return False
            
        logging.info("Attempting to install Node.js using winget...")
        # Check if winget is available first
        try:
            subprocess.run(['winget', '--version'], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logging.info("Winget not available or not responding, attempting manual Node.js installation...")
            try:
                # Download Node.js installer
                url = 'https://nodejs.org/dist/v20.11.1/node-v20.11.1-x64.msi'  # LTS version
                logging.info("Downloading Node.js installer...")
                response = requests.get(url, stream=True, timeout=30)
                
                # Save installer to temp file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.msi') as tmp_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            tmp_file.write(chunk)
                    installer_path = tmp_file.name
                
                # Run installer silently and wait for completion
                logging.info("Running Node.js installer (this may take a few minutes)...")
                process = subprocess.Popen(
                    ['msiexec', '/i', installer_path, '/qn', '/norestart'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True
                )
                
                # Wait for installation to complete with timeout
                try:
                    stdout, stderr = process.communicate(timeout=300)  # 5 minute timeout
                    if process.returncode == 0:
                        logging.info("Node.js MSI installation completed")
                    else:
                        logging.error(f"MSI installation failed with return code {process.returncode}")
                        if stderr:
                            logging.error(f"Installation error: {stderr}")
                        return False
                except subprocess.TimeoutExpired:
                    process.kill()
                    logging.error("Node.js installation timed out after 5 minutes")
                    return False
                
                # Clean up installer
                try:
                    os.unlink(installer_path)
                except Exception as e:
                    logging.warning(f"Failed to clean up installer file: {e}")
                
                # Verify installation by checking npm
                logging.info("Verifying Node.js installation...")
                for _ in range(5):  # Try up to 5 times with delays
                    try:
                        # Update PATH to include the new Node.js installation
                        os.environ['PATH'] = os.environ['PATH'] + ';C:\\Program Files\\nodejs'
                        verify_process = subprocess.run(
                            ['npm', '--version'],
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        if verify_process.returncode == 0:
                            logging.info(f"Node.js installation verified. npm version: {verify_process.stdout.strip()}")
                            return True
                    except Exception:
                        pass
                    time.sleep(2)  # Wait 2 seconds before retrying
                
                logging.error("Failed to verify Node.js installation")
                return False
                    
            except Exception as e:
                logging.error(f"Error during manual Node.js installation: {e}")
                return False
        
        # Install Node.js using winget with auto-accept
        try:
            logging.info("Installing Node.js (this may take a few minutes)...")
            process = subprocess.Popen(
                ['winget', 'install', '--id', 'OpenJS.NodeJS.LTS', '--source', 'winget', '--accept-package-agreements'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Line buffered
            )
            
            # Print output in real-time
            try:
                while True:
                    output = process.stdout.readline()
                    if output:
                        output = output.strip()
                        if output:  # Only log non-empty lines
                            logging.info(f"winget: {output}")
                            if "Downloading" in output:
                                logging.info("Download started - please wait...")
                    error = process.stderr.readline()
                    if error:
                        error = error.strip()
                        if error:  # Only log non-empty lines
                            logging.error(f"winget error: {error}")
                    # If process has finished and no more output, break
                    if output == '' and error == '' and process.poll() is not None:
                        break
            except KeyboardInterrupt:
                logging.warning("Installation interrupted by user")
                process.terminate()
                return False
            
            # Wait for winget to complete and verify installation
            if process.returncode == 0:
                logging.info("Node.js installed successfully via winget")
                # Verify installation
                logging.info("Verifying Node.js installation...")
                for _ in range(5):  # Try up to 5 times with delays
                    try:
                        # Update PATH to include the new Node.js installation
                        os.environ['PATH'] = os.environ['PATH'] + ';C:\\Program Files\\nodejs'
                        verify_process = subprocess.run(
                            ['npm', '--version'],
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        if verify_process.returncode == 0:
                            logging.info(f"Node.js installation verified. npm version: {verify_process.stdout.strip()}")
                            return True
                    except Exception:
                        pass
                    time.sleep(2)  # Wait 2 seconds before retrying
                
                logging.error("Failed to verify Node.js installation")
                return False
            else:
                logging.error(f"Failed to install Node.js with winget (exit code: {process.returncode})")
                # Fallback to manual installation
                logging.info("Falling back to manual installation...")
                return install_nodejs()  # Recursive call will try manual installation
        except subprocess.TimeoutExpired:
            logging.error("Winget installation timed out, falling back to manual installation...")
            return install_nodejs()  # Recursive call will try manual installation
            
    except Exception as e:
        logging.error(f"Error installing Node.js: {e}")
        return False

def run_phalanx_db():
    try:
        phalanx_dir = os.path.join(os.path.dirname(get_script_path('main.py')), 'phalanx_db_hyperswarm')
        
        # Check if phalanx_db_hyperswarm directory exists and is valid
        if not os.path.exists(os.path.join(phalanx_dir, 'package.json')):
            logging.error(f"phalanx_db_hyperswarm directory not found or invalid at {phalanx_dir}")
            return
        
        logging.info(f"Starting phalanx_db_hyperswarm service in {phalanx_dir}")
        
        # First run npm install if node_modules doesn't exist
        if not os.path.exists(os.path.join(phalanx_dir, 'node_modules')):
            logging.info("Installing phalanx_db_hyperswarm dependencies...")
            try:
                subprocess.run(['npm', 'install'], 
                             cwd=phalanx_dir,
                             shell=True,
                             check=True,
                             capture_output=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to install phalanx_db_hyperswarm dependencies: {e.stderr}")
                return
            except Exception as e:
                logging.error(f"Error during npm install: {str(e)}")
                return
            logging.info("Successfully installed phalanx_db_hyperswarm dependencies")

        # Start the service
        try:
            subprocess.run(['node', 'phalanx_db_rest.js'], 
                         cwd=phalanx_dir, 
                         shell=True,
                         check=True,
                         env=dict(os.environ))
        except subprocess.CalledProcessError as e:
            logging.error(f"phalanx_db_hyperswarm service failed to start: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        except Exception as e:
            logging.error(f"Failed to start phalanx_db_hyperswarm service: {str(e)}")
            logging.debug(traceback.format_exc())
    except Exception as e:
        logging.error(f"Unexpected error in phalanx_db_hyperswarm service: {str(e)}")
        logging.debug(traceback.format_exc())

def run_main():
    logging.info("Starting run_main()")
    
    # Start resource monitoring
    monitor_thread = start_resource_monitoring(interval=30)  # Log every 30 seconds
    
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '--cli-port', type=int, help='Port for remote access to main service (tunnels to local service)')
    parser.add_argument('--battery-port', '--cli-battery-port', type=int, help='Port for remote access to battery service (tunnels to local service)')
    parser.add_argument('--no-tunnel', action='store_true', help='Disable automatic tunneling')
    parser.add_argument('--enable-phalanx-db', action='store_true', help='Enable phalanx db integration')
    args = parser.parse_args()
    
    # Set up environment first to ensure we can read settings
    setup_environment()

    # Log the config path being used
    config_path_to_check = os.path.join(os.environ.get('USER_CONFIG', 'UNKNOWN_PATH'), 'config.json')
    logging.info(f"Checking for config at: {config_path_to_check}")
    logging.info(f"Config exists: {os.path.exists(config_path_to_check)}")

    # Check both command line argument and settings for phalanx db
    phalanx_setting_value = get_setting('UI Settings', 'enable_phalanx_db', default=None) # Use None default to distinguish from False
    logging.info(f"Value read from get_setting for enable_phalanx_db: {phalanx_setting_value}")
    
    phalanx_enabled = args.enable_phalanx_db or (phalanx_setting_value is True) # Explicitly check for True
    logging.info(f"Command line arg --enable-phalanx-db: {args.enable_phalanx_db}")
    logging.info(f"Final calculated phalanx_enabled value: {phalanx_enabled}")
    
    os.environ['ENABLE_PHALANX_DB'] = str(phalanx_enabled).lower()

    if phalanx_enabled:
        logging.info("Phalanx DB integration enabled")
        # Check Node.js installation before proceeding
        if not check_npm_available():
            logging.warning("npm is not installed. Installing Node.js (this may take a few minutes)...")
            if not install_nodejs():
                logging.error("Failed to install Node.js. Phalanx DB integration will be disabled.")
                phalanx_enabled = False
                os.environ['ENABLE_PHALANX_DB'] = 'false'
            else:
                logging.info("Node.js installed successfully.")
                # Verify npm is now available
                if not check_npm_available():
                    logging.error("Node.js installation verified but npm still not available. Phalanx DB integration will be disabled.")
                    phalanx_enabled = False
                    os.environ['ENABLE_PHALANX_DB'] = 'false'
    else:
        logging.info("Phalanx DB integration disabled")
    
    # On Windows, use different default ports and check availability
    if sys.platform.startswith('win'):
        ports = DEFAULT_PORTS['win32']
    else:
        ports = DEFAULT_PORTS['default']
        
    # Local service ports (always on localhost)
    main_port = ports['main']
    battery_port = ports['battery']
    
    # Check if local ports are available and find alternatives if needed
    if not is_port_available(main_port):
        logging.warning(f"Local port {main_port} is not available")
        main_port = find_available_port(main_port + 1)
        logging.info(f"Using alternative local port {main_port} for main server")
        
    if not is_port_available(battery_port):
        logging.warning(f"Local port {battery_port} is not available")
        battery_port = find_available_port(battery_port + 1)
        logging.info(f"Using alternative local port {battery_port} for battery server")
    
    # Always bind to localhost for security
    main_host = '127.0.0.1'
    battery_host = '127.0.0.1'
    
    # Update environment with actual local ports
    os.environ['CLI_DEBRID_PORT'] = str(main_port)
    os.environ['CLI_DEBRID_BATTERY_PORT'] = str(battery_port)
    
    # Start services
    processes = []

    # Add phalanx_db to services if enabled and Node.js is properly installed
    if phalanx_enabled:
        process = multiprocessing.Process(
            target=run_phalanx_db,
            name="Process-phalanx_db"
        )
        processes.append(process)
        process.start()
        logging.info("Started phalanx_db process")

    # Start both processes in parallel with appropriate ports
    script_names = ['main.py', os.path.join('cli_battery', 'main.py')]
    for script_name in script_names:
        if 'cli_battery' in script_name:
            process = multiprocessing.Process(
                target=run_script, 
                args=(script_name,),
                kwargs={
                    'battery_port': battery_port,
                    'host': battery_host
                },
                name=f"Process-{script_name}"
            )
            logging.info(f"Starting battery process on {battery_host}:{battery_port}")
        else:
            process = multiprocessing.Process(
                target=run_script, 
                args=(script_name,),
                kwargs={
                    'port': main_port,
                    'host': main_host
                },
                name=f"Process-{script_name}"
            )
            logging.info(f"Starting main process on {main_host}:{main_port}")
        
        processes.append(process)
        process.start()

    # Wait for services to be ready
    logging.info("Waiting for services to be ready...")
    main_ready = is_service_ready(main_port)
    battery_ready = is_service_ready(battery_port)
    
    if not main_ready:
        logging.error(f"Main service failed to start on port {main_port}")
    if not battery_ready:
        logging.error(f"Battery service failed to start on port {battery_port}")
    
    # Set up tunnels only if services are ready
    tunnel_threads = []
    if not args.no_tunnel and (main_ready or battery_ready):
        # Use provided ports or defaults for tunneling
        tunnel_main_port = args.port if args.port else ports['tunnel_main']
        tunnel_battery_port = args.battery_port if args.battery_port else ports['tunnel_battery']
        
        # Update environment with tunnel ports
        os.environ['CLI_DEBRID_TUNNEL_PORT'] = str(tunnel_main_port)
        os.environ['CLI_DEBRID_BATTERY_TUNNEL_PORT'] = str(tunnel_battery_port)
        
        if main_ready and is_port_available(tunnel_main_port):
            tunnel_threads.append(create_tunnel(tunnel_main_port, main_port))
            logging.info(f"Created tunnel from port {tunnel_main_port} to main service on {main_port}")
        else:
            logging.error(f"Cannot create tunnel for main service: Service ready: {main_ready}, Port available: {is_port_available(tunnel_main_port)}")
            
        if battery_ready and is_port_available(tunnel_battery_port):
            tunnel_threads.append(create_tunnel(tunnel_battery_port, battery_port))
            logging.info(f"Created tunnel from port {tunnel_battery_port} to battery service on {battery_port}")
        else:
            logging.error(f"Cannot create tunnel for battery service: Service ready: {battery_ready}, Port available: {is_port_available(tunnel_battery_port)}")
    else:
        # If tunneling is disabled, remove tunnel port environment variables
        os.environ.pop('CLI_DEBRID_TUNNEL_PORT', None)
        os.environ.pop('CLI_DEBRID_BATTERY_TUNNEL_PORT', None)

    # Set up process monitoring
    def monitor_processes():
        while any(p.is_alive() for p in processes):
            for i, p in enumerate(processes):
                if p.is_alive():
                    try:
                        proc = psutil.Process(p.pid)
                        mem_info = proc.memory_info()
                        logging.info(f"Process {p.name} (PID {p.pid}) - Memory: {mem_info.rss / (1024 * 1024):.2f} MB, CPU: {proc.cpu_percent(interval=0.1)}%")
                    except Exception as e:
                        logging.error(f"Error monitoring process {p.name}: {str(e)}")
            time.sleep(60)  # Check every minute
    
    monitor_proc_thread = threading.Thread(target=monitor_processes, name="ProcessMonitor")
    monitor_proc_thread.daemon = True
    monitor_proc_thread.start()

    try:
        # Wait for all processes to complete
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received. Terminating scripts.")
        for process in processes:
            if process.is_alive():
                try:
                    process.terminate()
                    process.join(timeout=5)  # Wait up to 5 seconds
                    if process.is_alive():
                        logging.warning(f"Process {process.name} did not terminate gracefully, killing it")
                        process.kill()
                        process.join(timeout=2)
                except Exception as e:
                    logging.error(f"Error terminating process {process.name}: {str(e)}")
    finally:
        # Final resource log before exit
        log_system_resources()
        logging.info("All processes have exited")

if __name__ == "__main__":
    logging.info("Starting cli_debrid.exe")
    try:
        multiprocessing.freeze_support()
        adjust_sys_path()
        run_main()
    except Exception as e:
        logging.error(f"Unhandled exception: {str(e)}")
        logging.error(traceback.format_exc())
    finally:
        # Force garbage collection before exit
        gc.collect()
        logging.info("cli_debrid.exe execution completed")
    
    # Keep console window open
    input("Press Enter to exit...")
