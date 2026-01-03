import json
import os
import subprocess
import time

def is_limited_environment():
    """Check if we're running in limited environment mode"""
    env_mode = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full')
    return env_mode != 'full'

def load_config():
    # Check environment mode first
    env_mode = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full')
    if env_mode != 'full':
        return False
    
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

def wait_for_supervisord_ready(max_wait=10):
    """Wait for supervisord to be fully ready before issuing commands"""
    for i in range(max_wait):
        try:
            result = subprocess.run(['supervisorctl', 'status'],
                                  capture_output=True,
                                  text=True,
                                  timeout=2)
            if result.returncode == 0:
                print(f"Supervisord ready after {i+1} seconds")
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(1)
    print("Warning: Supervisord may not be fully ready")
    return False

def get_process_state(process_name):
    """Get the current state of a supervisord process"""
    try:
        result = subprocess.run(['supervisorctl', 'status', process_name],
                              capture_output=True,
                              text=True,
                              timeout=2)
        output = result.stdout.strip()
        if 'RUNNING' in output:
            return 'RUNNING'
        elif 'STOPPED' in output:
            return 'STOPPED'
        elif 'STARTING' in output:
            return 'STARTING'
        return 'UNKNOWN'
    except Exception as e:
        print(f"Failed to get process state for {process_name}: {e}")
        return 'UNKNOWN'

def control_secondary_app(enable):
    """Control secondary_app (cli_battery) with proper state checking"""
    # Wait for supervisord to be ready
    wait_for_supervisord_ready()

    # Additional delay to ensure autostart processes have begun
    time.sleep(3)

    try:
        current_state = get_process_state('secondary_app')
        print(f"secondary_app current state: {current_state}, target: {'RUNNING' if enable else 'STOPPED'}")

        if enable:
            # In full mode, we want it running
            # If it's already running or starting, don't interfere
            if current_state in ['RUNNING', 'STARTING']:
                print("secondary_app already running/starting, no action needed")
                return
            # Only start if it's stopped
            subprocess.run(['supervisorctl', 'start', 'secondary_app'], check=True)
            print("secondary_app started successfully")
        else:
            # In limited mode, we want it stopped
            if current_state == 'STOPPED':
                print("secondary_app already stopped, no action needed")
                return
            # Stop it if running
            subprocess.run(['supervisorctl', 'stop', 'secondary_app'], check=True)
            print("secondary_app stopped successfully")
    except subprocess.CalledProcessError as e:
        print(f"Failed to control secondary_app: {e}")

if __name__ == '__main__':
    enable_phalanx = load_config()
    limited_env = is_limited_environment()
    # Still write the environment file for the program to use if needed
    with open('/tmp/supervisor_env', 'w') as f:
        f.write(f"ENABLE_PHALANX_DB={'true' if enable_phalanx else 'false'}")
    # Control the program state directly
    control_phalanx_db(enable_phalanx)
    # If in limited environment, stop the secondary_app (battery)
    control_secondary_app(not limited_env) 