import logging
import os
import shutil
import signal
import sys
import time
from api_tracker import api
from settings import get_setting
import requests
import grpc
import metadata_service_pb2
import metadata_service_pb2_grpc
import re
from settings import set_setting


def setup_logging():
    logging.getLogger('selector').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    # Ensure logs directory exists
    if not os.path.exists('/user/logs'):
        os.makedirs('/user/logs')

    # Ensure log files exist
    for log_file in ['debug.log', 'info.log', 'queue.log']:
        log_path = os.path.join('/user/logs', log_file)
        if not os.path.exists(log_path):
            with open(log_path, 'w'):
                pass

    import logging_config
    logging_config.setup_logging()

def setup_directories():
    # Ensure db_content directory exists
    if not os.path.exists('/user/db_content'):
        os.makedirs('/user/db_content')

def backup_config():
    config_path = '/user/config/config.json'
    if os.path.exists(config_path):
        backup_path = f'/user/config/config_backup.json'
        shutil.copy2(config_path, backup_path)
        logging.info(f"Backup of config.json created: {backup_path}")
    else:
        logging.warning("config.json not found, no backup created.")

def get_version():
    try:
        with open('version.txt', 'r') as version_file:
            version = version_file.read().strip()
    except FileNotFoundError:
        version = "0.0.0"
    return version

def signal_handler(signum, frame):
    global program_runner
    if program_runner:
        os.system('clear')
        print("\nStopping the program...")
        program_runner.stop()
        print("Program stopped.")
    else:
        print("\nProgram stopped.")
    sys.exit(0)

def update_web_ui_state(state):
    try:
        api.post('http://localhost:5000/api/update_program_state', json={'state': state})
    except api.exceptions.RequestException:
        logging.error("Failed to update web UI state")

def check_metadata_service():
    grpc_url = get_setting('Metadata Battery', 'url')
    
    # Remove leading "http://" or "https://"
    grpc_url = re.sub(r'^https?://', '', grpc_url)
    
    # Remove trailing ":5000" or ":5000/" if present
    grpc_url = grpc_url.rstrip('/').removesuffix(':5001')
    grpc_url = grpc_url.rstrip('/').removesuffix(':50051')
    
    # Append ":50051"
    grpc_url += ':50051'
    
    try:
        channel = grpc.insecure_channel(grpc_url)
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        # Try to make a simple call to check connectivity
        stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id="1"), timeout=5)
        logging.info(f"Successfully connected to metadata service at {grpc_url}")
        return grpc_url
    except grpc.RpcError:
        logging.warning(f"Failed to connect to {grpc_url}, falling back to localhost")
        fallback_urls = ['localhost:50051', 'cli_battery_app:50051']
        for url in fallback_urls:
            try:
                channel = grpc.insecure_channel(url)
                stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
                stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id="1"), timeout=5)
                logging.info(f"Successfully connected to metadata service at {url}")
                return url
            except grpc.RpcError:
                logging.warning(f"Failed to connect to metadata service at {url}")
        logging.error("Failed to connect to metadata service on all fallback options")
        return None

def main():
    global program_runner
    setup_logging()
    setup_directories()
    backup_config()
    
    from settings import ensure_settings_file
    from database import verify_database
    
    ensure_settings_file()
    verify_database()
    
    os.system('clear')
    
    version = get_version()
    
    # Display logo and web UI message
    import socket
    ip_address = socket.gethostbyname(socket.gethostname())
    print(f"""
      (            (             )           (     
      )\ (         )\ )   (   ( /(  (   (    )\ )  
  (  ((_))\       (()/(  ))\  )\()) )(  )\  (()/(  
  )\  _ ((_)       ((_))/((_)((_)\ (()\((_)  ((_)) 
 ((_)| | (_)       _| |(_))  | |(_) ((_)(_)  _| |  
/ _| | | | |     / _` |/ -_) | '_ \| '_|| |/ _` |  
\__| |_| |_|_____\__,_|\___| |_.__/|_|  |_|\__,_|  
           |_____|                                 

           Version: {version}                      
    """)
    print(f"cli_debrid is initialized.")
    print(f"The web UI is available at http://{ip_address}:5000")
    print("Use the web UI to control the program.")
    print("Press Ctrl+C to stop the program.")

    # Check metadata service connectivity
    metadata_url = check_metadata_service()
    if metadata_url:
        set_setting('Metadata Battery', 'url', metadata_url)
    else:
        print("Failed to connect to metadata service. Please check your configuration.")
        sys.exit(1)

    if get_setting('Debug', 'auto_run_program'):
        # Call the start_program route
        try:
            response = requests.post('http://localhost:5000/program_operation/api/start_program')
            if response.status_code == 200:
                print("Program started successfully")
            else:
                print(f"Failed to start program. Status code: {response.status_code}")
                print(f"Response: {response.text}")
        except requests.RequestException as e:
            print(f"Error calling start_program route: {e}")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep the script running
    while True:
        time.sleep(1)

if __name__ == "__main__":
    from api_tracker import setup_api_logging
    setup_api_logging()
    from web_server import start_server
    start_server()
    main()