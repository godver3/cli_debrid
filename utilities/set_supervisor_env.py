import json
import os
import subprocess
import time

def load_config():
    try:
        with open('/user/config/config.json', 'r') as f:
            config = json.load(f)
            return config.get('UI Settings', {}).get('enable_phalanx_db', False)
    except Exception:
        # Default to False if config can't be read
        return False

def control_phalanx_db(enable):
    # Give supervisord time to start up
    time.sleep(2)
    try:
        if enable:
            subprocess.run(['supervisorctl', 'start', 'phalanx_db'], check=True)
        else:
            subprocess.run(['supervisorctl', 'stop', 'phalanx_db'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to control phalanx_db: {e}")

if __name__ == '__main__':
    enable_phalanx = load_config()
    # Still write the environment file for the program to use if needed
    with open('/tmp/supervisor_env', 'w') as f:
        f.write(f"ENABLE_PHALANX_DB={'true' if enable_phalanx else 'false'}")
    # Control the program state directly
    control_phalanx_db(enable_phalanx) 