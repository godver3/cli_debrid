from flask import current_app
import logging
from settings import get_setting

def is_user_system_enabled():
    enable_user_system = get_setting('UI Settings', 'enable_user_system', False)
    logging.debug(f"UI Settings - enable_user_system: {enable_user_system}")
    return enable_user_system