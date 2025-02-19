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

# Default ports configuration
DEFAULT_PORTS = {
    'win32': {
        'main': 8585,
        'battery': 8586,
        'tunnel_main': 5000,
        'tunnel_battery': 5001
    },
    'default': {
        'main': 5000,
        'battery': 5001,
        'tunnel_main': 5000,
        'tunnel_battery': 5001
    }
}

# Set up logging to write to a file
log_file = os.path.join(
    os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(),
    'cli_debrid.log'
)
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Also print to console
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

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

def create_tunnel(remote_port, local_port, buffer_size=4096):
    """Creates a tunnel from a remote port to a local port."""
    def handle_client(client_sock, local_port):
        try:
            # Connect to local service
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            local_sock.connect(('127.0.0.1', local_port))
            
            while True:
                # Wait for data from either socket
                readable, _, exceptional = select.select([client_sock, local_sock], [], [client_sock, local_sock], 60)
                
                if exceptional:
                    break
                
                for sock in readable:
                    other_sock = local_sock if sock is client_sock else client_sock
                    try:
                        data = sock.recv(buffer_size)
                        if not data:
                            return
                        other_sock.sendall(data)
                    except:
                        return
        except:
            logging.error(f"Error in tunnel connection: {traceback.format_exc()}")
        finally:
            try:
                client_sock.close()
                local_sock.close()
            except:
                pass

    def tunnel_server():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(('0.0.0.0', remote_port))
            server.listen(5)
            logging.info(f"Port forwarding active: {remote_port} -> 127.0.0.1:{local_port}")
            
            while True:
                try:
                    client_sock, addr = server.accept()
                    logging.info(f"New tunnel connection from {addr}")
                    client_thread = threading.Thread(
                        target=handle_client,
                        args=(client_sock, local_port)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except:
                    logging.error(f"Error accepting connection: {traceback.format_exc()}")
        except:
            logging.error(f"Error in tunnel server: {traceback.format_exc()}")
        finally:
            try:
                server.close()
            except:
                pass

    tunnel_thread = threading.Thread(target=tunnel_server)
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

def run_main():
    logging.info("Starting run_main()")
    
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '--cli-port', type=int, help='Port for remote access to main service (tunnels to local service)')
    parser.add_argument('--battery-port', '--cli-battery-port', type=int, help='Port for remote access to battery service (tunnels to local service)')
    parser.add_argument('--no-tunnel', action='store_true', help='Disable automatic tunneling')
    args = parser.parse_args()
    
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
    
    # Set up tunnels unless explicitly disabled
    tunnel_threads = []
    if not args.no_tunnel:
        # Use provided ports or defaults for tunneling
        tunnel_main_port = args.port if args.port else ports['tunnel_main']
        tunnel_battery_port = args.battery_port if args.battery_port else ports['tunnel_battery']
        
        # Update environment with tunnel ports
        os.environ['CLI_DEBRID_TUNNEL_PORT'] = str(tunnel_main_port)
        os.environ['CLI_DEBRID_BATTERY_TUNNEL_PORT'] = str(tunnel_battery_port)
        
        if is_port_available(tunnel_main_port):
            tunnel_threads.append(create_tunnel(tunnel_main_port, main_port))
            logging.info(f"Created tunnel from port {tunnel_main_port} to main service on {main_port}")
        else:
            logging.error(f"Tunnel port {tunnel_main_port} is not available")
            
        if is_port_available(tunnel_battery_port):
            tunnel_threads.append(create_tunnel(tunnel_battery_port, battery_port))
            logging.info(f"Created tunnel from port {tunnel_battery_port} to battery service on {battery_port}")
        else:
            logging.error(f"Tunnel port {tunnel_battery_port} is not available")
    else:
        # If tunneling is disabled, remove tunnel port environment variables
        os.environ.pop('CLI_DEBRID_TUNNEL_PORT', None)
        os.environ.pop('CLI_DEBRID_BATTERY_TUNNEL_PORT', None)
    
    # Log binding information
    logging.info(f"Main service binding to: {main_host}:{main_port}")
    logging.info(f"Battery service binding to: {battery_host}:{battery_port}")
    
    script_names = ['main.py', os.path.join('cli_battery', 'main.py')]
    processes = []

    # Start both processes in parallel with appropriate ports
    for script_name in script_names:
        if 'cli_battery' in script_name:
            process = multiprocessing.Process(
                target=run_script, 
                args=(script_name,),
                kwargs={
                    'battery_port': battery_port,
                    'host': battery_host
                }
            )
            logging.info(f"Starting battery process on {battery_host}:{battery_port}")
        else:
            process = multiprocessing.Process(
                target=run_script, 
                args=(script_name,),
                kwargs={
                    'port': main_port,
                    'host': main_host
                }
            )
            logging.info(f"Starting main process on {main_host}:{main_port}")
        
        processes.append(process)
        process.start()

    try:
        # Wait for both processes to complete
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received. Terminating scripts.")
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()

if __name__ == "__main__":
    logging.info("Starting cli_debrid.exe")
    try:
        multiprocessing.freeze_support()
        adjust_sys_path()
        run_main()
    except Exception as e:
        logging.error(f"Unhandled exception: {str(e)}")
        logging.error(traceback.format_exc())
    logging.info("cli_debrid.exe execution completed")
    
    # Keep console window open
    input("Press Enter to exit...")
