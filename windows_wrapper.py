import os
import sys
import threading

def setup_paths():
    base_path = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    user_dirs = ['logs', 'db_content', 'config']

    for dir_name in user_dirs:
        path = os.path.join(base_path, 'user', dir_name)
        os.makedirs(path, exist_ok=True)

def run_main_app():
    os.system(f'"{sys.executable}" main.py')

def run_battery_app():
    os.system(f'"{sys.executable}" cli_battery{os.sep}main.py')

if __name__ == "__main__":
    setup_paths()

    main_thread = threading.Thread(target=run_main_app)
    battery_thread = threading.Thread(target=run_battery_app)

    main_thread.start()
    battery_thread.start()

    main_thread.join()
    battery_thread.join()
