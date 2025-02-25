import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from settings_schema import SETTINGS_SCHEMA
from collections import defaultdict
from datetime import datetime
import time
from threading import Timer, Lock
import sys
import json
from pathlib import Path
import os
import uuid

# Global notification buffer
notification_buffer = []
notification_timer = None
safety_valve_timer = None
buffer_lock = Lock()
BUFFER_TIMEOUT = 10  # seconds to wait before sending notifications
SAFETY_VALVE_TIMEOUT = 60  # seconds maximum to wait before forcing send

# Constants
MAX_NOTIFICATIONS = 20
NOTIFICATION_FILE = Path(os.getenv('USER_DB_CONTENT', '/user/db_content')) / 'notifications.json'

def safe_format_date(date_value):
    if not date_value:
        return "Unknown"
    try:
        if isinstance(date_value, str):
            return datetime.fromisoformat(date_value).strftime('%Y-%m-%d')
        elif isinstance(date_value, datetime):
            return date_value.strftime('%Y-%m-%d')
        else:
            logging.warning(f"Unexpected date type: {type(date_value)}")
            return "Unknown"
    except Exception as e:
        logging.warning(f"Error formatting date {date_value}: {str(e)}")
        return "Unknown"

def escape_discord_formatting(text):
    """Escape Discord's Markdown formatting characters in text."""
    return text.replace('*', '\\*')

def consolidate_items(notifications):
    consolidated = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    
    # First pass: organize all items
    for notification in sorted(notifications, key=lambda x: (
        x['title'],
        x.get('season_number', ''),
        x.get('episode_number', '')
    )):
        media_type = notification['type']
        title = notification['title']
        year = notification.get('year', '')
        version = notification.get('version', 'Default')
        is_upgrade = notification.get('is_upgrade', False)
        original_collected_at = notification.get('original_collected_at')
        
        key = f"{title} ({year})"
        item_info = {
            'version': version,
            'is_upgrade': is_upgrade,
            'original_collected_at': original_collected_at
        }
        
        # Create a unique identifier for the item
        if media_type == 'episode':
            season_number = notification.get('season_number', '')
            episode_number = notification.get('episode_number', '')
            item_info['episode'] = f"S{season_number:02d}E{episode_number:02d}"
            season_key = f"Season {season_number}"
            consolidated['show'][key][season_key].append(item_info)
        elif media_type == 'season':
            season_number = notification.get('season_number', '')
            item_info['season'] = f"Season {season_number}"
            consolidated['show'][key]['seasons'].append(item_info)
        else:
            # For movies, we'll combine versions for the same item
            existing_items = consolidated['movie'][key]['items']
            # Check if we already have an item with the same upgrade status and collection date
            matching_item = next((
                item for item in existing_items 
                if item['is_upgrade'] == is_upgrade and 
                item['original_collected_at'] == original_collected_at
            ), None)
            
            if matching_item:
                # If we find a matching item, just add the version if it's new
                if version not in matching_item['versions']:
                    matching_item['versions'].append(version)
            else:
                # If no matching item, create a new one with versions as a list
                item_info['versions'] = [version]
                consolidated['movie'][key]['items'].append(item_info)
            
    return consolidated

def format_notification_content(notifications, notification_type, notification_category='collected'):
    # Define emojis for all notification types
    EMOJIS = {
        'movie': "🎬",
        'show': "📺",
        'upgrade': "⬆️",
        'new': "🆕",
        'program_stop': "🛑",
        'program_crash': "💥",
        'program_start': "🟢",
        'queue_pause': "⚠️",
        'queue_resume': "✅",
        'queue_start': "▶️",
        'queue_stop': "⏹️",
        'upgrade_failed': "❌"  # New emoji for failed upgrades
    }

    # For system notifications (stop/crash/start/pause/resume), we'll use a different format
    if notification_category in ['program_stop', 'program_crash', 'program_start', 'queue_pause', 'queue_resume', 'queue_start', 'queue_stop', 'upgrade_failed']:
        emoji = EMOJIS.get(notification_category, "ℹ️")
        if notification_category == 'upgrade_failed':
            # Special formatting for failed upgrades
            if isinstance(notifications, dict):
                title = notifications.get('title', 'Unknown')
                year = notifications.get('year', '')
                reason = notifications.get('reason', 'Unknown reason')
                return f"{emoji} **Upgrade Failed**\nTitle: {title} ({year})\nReason: {reason}"
        return f"{emoji} **cli_debrid {notification_category.replace('_', ' ').title()}**\n{notifications}"

    def format_state_suffix(state, is_upgrade=False):
        """Return the appropriate suffix based on state"""
        if state == 'Collected' and is_upgrade:
            return f"→ Upgraded"
        else:
            return f"→ {state}"

    def format_title(item):
        """Format the title with appropriate prefix and formatting."""
        from settings import get_setting
        enable_detailed_info = get_setting('Debug', 'enable_detailed_notification_information', False)
        
        title = item.get('title', '')
        year = item.get('year', '')
        version = item.get('version', '').strip('*')
        is_upgrade = item.get('is_upgrade', False)
        media_type = item.get('type', 'movie')
        new_state = item.get('new_state', '')
        content_source = item.get('content_source')
        content_source_detail = item.get('content_source_detail')
        filled_by_file = item.get('filled_by_file')
        
        # Choose prefix based on state and upgrade status
        if new_state == 'Downloading':
            prefix = "⬇️"  # Download emoji for downloading state
        elif new_state == 'Checking':
            prefix = EMOJIS['show'] if media_type == 'episode' else EMOJIS['movie']
        elif new_state == 'Upgrading':
            prefix = EMOJIS['movie'] if media_type == 'movie' else EMOJIS['show']
        elif new_state == 'Upgraded':
            prefix = EMOJIS['upgrade']
        elif new_state == 'Collected':
            prefix = EMOJIS['new']
        else:
            prefix = EMOJIS['show'] if media_type == 'episode' else EMOJIS['movie']
        
        # Base title format
        formatted_title = f"{prefix} **{title}** ({year})"
        
        # Add version info for movies
        if media_type == 'movie':
            formatted_title += f" [{version}]"
            
        # Add content source information if enabled and available for collected or upgraded items
        if enable_detailed_info and (new_state == 'Collected' or new_state == 'Upgraded'):
            if content_source:
                formatted_title += f"\nSource: {content_source}"
            if content_source_detail:
                formatted_title += f"\nRequested by: {content_source_detail}"
            if filled_by_file:
                formatted_title += f"\nFile: {filled_by_file}"
                
        return formatted_title

    def format_episode(item):
        """Format episode information"""
        try:
            season = item.get('season_number')
            episode = item.get('episode_number')
            if season is not None and episode is not None:
                # Convert to integers and handle potential string inputs
                season = int(season)
                episode = int(episode)
                version = item.get('version', '')
                version_str = f" [{version}]" if version else ""
                return f"    S{season:02d}E{episode:02d}{version_str}"
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid season/episode format: {str(e)} - S:{season} E:{episode}")
        return None

    # Group items by show/movie
    grouped_items = {}
    for item in notifications:
        # Group by title, type, year, and state for better batching
        key = (item.get('title'), item.get('type'), item.get('year'), item.get('new_state'), item.get('is_upgrade', False))
        if key not in grouped_items:
            grouped_items[key] = []
        grouped_items[key].append(item)

    content = []
    
    # Process each group
    for (title, type_, year, state, is_upgrade), items in sorted(grouped_items.items()):
        # Create a representative item for the group
        group_item = items[0].copy()
        
        # Add the title line only once per group
        content.append(format_title(group_item))
        
        # Sort episodes by season and episode number
        if type_ == 'episode':
            sorted_items = sorted(items, key=lambda x: (x.get('season_number', 0), x.get('episode_number', 0)))
            for item in sorted_items:
                episode_line = format_episode(item)
                if episode_line:
                    content.append(f"{episode_line} {format_state_suffix(state, is_upgrade)}")
        else:
            # For movies, just add the state suffix to the title line
            content[-1] = f"{content[-1]} {format_state_suffix(state, is_upgrade)}"

    # Join with single newlines between items
    return "\n".join(content)

def start_safety_valve_timer(enabled_notifications, notification_category):
    global safety_valve_timer
    
    if safety_valve_timer is not None:
        try:
            safety_valve_timer.cancel()
        except Exception as e:
            logging.error(f"Error cancelling safety valve timer: {str(e)}")
    
    safety_valve_timer = Timer(SAFETY_VALVE_TIMEOUT, force_flush_notification_buffer, args=[enabled_notifications, notification_category])
    safety_valve_timer.daemon = True  # Make it a daemon thread so it doesn't prevent program exit
    safety_valve_timer.start()

def force_flush_notification_buffer(enabled_notifications, notification_category):
    """Force flush the notification buffer regardless of normal buffering logic"""
    global notification_buffer, safety_valve_timer
    
    try:
        with buffer_lock:
            if notification_buffer:
                logging.info("Safety valve triggered - forcing notification flush")
                try:
                    _send_notifications(notification_buffer, enabled_notifications, notification_category)
                    notification_buffer = []
                except Exception as e:
                    logging.error(f"Failed to send notifications in safety valve: {str(e)}")
    except Exception as e:
        logging.error(f"Error in force_flush_notification_buffer: {str(e)}")
    finally:
        # Restart the safety valve timer
        start_safety_valve_timer(enabled_notifications, notification_category)

def buffer_notifications(notifications, enabled_notifications, notification_category='collected'):
    global notification_timer, notification_buffer
    
    try:
        with buffer_lock:
            # Add new notifications to buffer
            notification_buffer.extend(notifications)
            
            # Cancel existing timer if there is one
            if notification_timer is not None:
                try:
                    notification_timer.cancel()
                except Exception as e:
                    logging.error(f"Error cancelling timer: {str(e)}")
            
            # Set new timer
            notification_timer = Timer(BUFFER_TIMEOUT, flush_notification_buffer, args=[enabled_notifications, notification_category])
            notification_timer.start()
            
            # Ensure safety valve timer is running
            start_safety_valve_timer(enabled_notifications, notification_category)
    except Exception as e:
        logging.error(f"Error in buffer_notifications: {str(e)}")
        # Try to send immediately if buffering fails
        _send_notifications(notifications, enabled_notifications, notification_category)

def flush_notification_buffer(enabled_notifications, notification_category):
    global notification_buffer
    
    try:
        with buffer_lock:
            if notification_buffer:
                try:
                    # Send all buffered notifications
                    _send_notifications(notification_buffer, enabled_notifications, notification_category)
                    # Only clear if sending was successful
                    notification_buffer = []
                except Exception as e:
                    logging.error(f"Failed to send notifications: {str(e)}")
                    # Don't clear buffer on error to allow retry
    except Exception as e:
        logging.error(f"Error in flush_notification_buffer: {str(e)}")

def store_notification(title, message, notification_type='info', link=None):
    """Store a notification in the JSON file"""
    try:
        # Ensure directory exists
        NOTIFICATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        # Read existing notifications
        notifications = {"notifications": []}
        if NOTIFICATION_FILE.exists():
            try:
                with open(NOTIFICATION_FILE) as f:
                    notifications = json.load(f)
            except json.JSONDecodeError:
                logging.error("Invalid JSON in notifications file, starting fresh")
        
        # Create new notification
        new_notification = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "title": title,
            "message": message,
            "type": notification_type,
            "read": False
        }
        if link:
            new_notification["link"] = link
            
        # Add new notification and keep only the most recent MAX_NOTIFICATIONS
        notifications["notifications"].insert(0, new_notification)
        notifications["notifications"] = notifications["notifications"][:MAX_NOTIFICATIONS]
        
        # Write back to file
        with open(NOTIFICATION_FILE, 'w') as f:
            json.dump(notifications, f, indent=2)
            
    except Exception as e:
        logging.error(f"Error storing notification: {str(e)}", exc_info=True)

def _send_notifications(notifications, enabled_notifications, notification_category=None):
    successful = True  # Track if all notifications were sent successfully
    
    # Store notification in JSON file first
    try:
        # Handle system operation notifications
        if notification_category in ['program_crash', 'program_stop', 'program_start', 
                                   'queue_pause', 'queue_resume', 'queue_start', 'queue_stop',
                                   'rate_limit', 'scraping_error', 'content_error', 'database_error']:
            title = {
                'program_crash': "Program Crashed",
                'program_stop': "Program Stopped",
                'program_start': "Program Started",
                'queue_pause': "Queue Paused",
                'queue_resume': "Queue Resumed",
                'queue_start': "Queue Started",
                'queue_stop': "Queue Stopped",
                'rate_limit': "Rate Limit Warning",
                'scraping_error': "Scraping Error",
                'content_error': "Content Error",
                'database_error': "Database Error"
            }.get(notification_category)

            notification_type = {
                'program_crash': 'error',
                'program_stop': 'info',
                'program_start': 'success',
                'queue_pause': 'warning',
                'queue_resume': 'success',
                'queue_start': 'success',
                'queue_stop': 'info',
                'rate_limit': 'warning',
                'scraping_error': 'error',
                'content_error': 'error',
                'database_error': 'error'
            }.get(notification_category)

            message = notifications if isinstance(notifications, str) else {
                'program_crash': "Program crashed unexpectedly",
                'program_stop': "Program has been stopped",
                'program_start': "Program has been started",
                'queue_pause': "Queue processing has been paused",
                'queue_resume': "Queue processing has been resumed",
                'queue_start': "Queue processing has started",
                'queue_stop': "Queue processing has stopped",
                'rate_limit': "API rate limit warning",
                'scraping_error': "Error occurred during scraping",
                'content_error': "Error processing content",
                'database_error': "Database operation failed"
            }.get(notification_category)

            store_notification(title, message, notification_type)
            
        # Handle upgrade failed notifications
        elif notification_category == 'upgrade_failed':
            if isinstance(notifications, dict):
                title = "Upgrade Failed"
                message = f"Failed to upgrade {notifications.get('title', 'Unknown')} ({notifications.get('year', '')}): {notifications.get('reason', 'Unknown reason')}"
                store_notification(title, message, 'error', link="/queues")

        # Handle content notifications (collected, upgraded, etc.)
        else:
            # For each notification item
            for notification in notifications:
                title = notification.get('title', '')
                year = notification.get('year', '')
                version = notification.get('version', '').strip('*')
                media_type = notification.get('type', 'movie')
                new_state = notification.get('new_state', '')
                
                # Base message format
                message = f"{title} ({year})"
                if media_type == 'episode':
                    message += f" S{notification.get('season_number', '00')}E{notification.get('episode_number', '00')}"
                if version:
                    message += f" [{version}]"
                
                # Determine notification type and title based on state and data
                if new_state == 'Downloading':
                    notification_title = "Downloading Content"
                    notification_type = 'info'
                    message = f"Started downloading {message}"
                elif new_state == 'Checking':
                    notification_title = "Checking Content"
                    notification_type = 'info'
                    message = f"Checking {message}"
                elif new_state == 'Upgrading':
                    notification_title = "Upgrading Content"
                    notification_type = 'info'
                    message = f"Upgrading {message}"
                    if notification.get('upgrading_from'):
                        message += f"\nUpgrading from: {notification['upgrading_from']}"
                elif new_state == 'Upgraded':
                    notification_title = "Content Upgraded"
                    notification_type = 'success'
                    message = f"Successfully upgraded {message}"
                else:
                    # Default collection notification
                    notification_title = "New Content Available"
                    notification_type = 'success'
                    if notification.get('is_upgrade'):
                        message = f"Upgraded and collected {message}"
                    else:
                        message = f"Successfully collected {message}"
                
                # Add source information if available
                if notification.get('content_source'):
                    message += f"\nSource: {notification['content_source']}"
                if notification.get('content_source_detail'):
                    message += f"\nRequested by: {notification['content_source_detail']}"
                if notification.get('filled_by_file'):
                    message += f"\nFile: {notification['filled_by_file']}"
                
                # Store the notification
                store_notification(notification_title, message, notification_type, link="/queues")

    except Exception as e:
        logging.error(f"Error storing notification in JSON: {str(e)}", exc_info=True)
        successful = False

    # Only attempt to send notifications if JSON storage was successful
    if not successful:
        return successful

    for notification_id, notification_config in enabled_notifications.items():
        if not notification_config.get('enabled', False):
            continue

        notify_on = notification_config.get('notify_on', {})
        if not notify_on.get(notification_category, True):
            logging.debug(f"Skipping {notification_id} notification: {notification_category} notifications are disabled")
            continue

        notification_type = notification_config['type']
        
        try:
            content = format_notification_content(notifications, notification_type, notification_category)
        except Exception as e:
            logging.error(f"Failed to format notification content for {notification_type}: {str(e)}")
            successful = False
            continue

        try:
            if notification_type == 'Discord':
                webhook_url = notification_config.get('webhook_url')
                if not webhook_url:
                    logging.warning(f"Skipping Discord notification: webhook URL is empty")
                    continue
                send_discord_notification(webhook_url, content)
            
            elif notification_type == 'Email':
                if not all([notification_config.get(field) for field in ['smtp_server', 'smtp_port', 'smtp_username', 
                            'smtp_password', 'from_address', 'to_address']]):
                    logging.warning(f"Skipping Email notification: one or more required fields are empty")
                    continue
                send_email_notification(notification_config, content)
            
            elif notification_type == 'Telegram':
                bot_token = notification_config.get('bot_token')
                chat_id = notification_config.get('chat_id')
                if not bot_token or not chat_id:
                    logging.warning(f"Skipping Telegram notification: bot token or chat ID is empty")
                    continue
                send_telegram_notification(bot_token, chat_id, content)

            elif notification_type == 'NTFY':
                host = notification_config.get('host')
                api_key = notification_config.get('api_key')
                priority = notification_config.get('priority')
                topic = notification_config.get('topic')
                if not host or not topic:
                    logging.warning(f"Skipping NTFY notification: host or topic is empty")
                    continue
                send_ntfy_notification(host, api_key, priority, topic, content)
            
            else:
                logging.warning(f"Unknown notification type: {notification_type}")
                successful = False
                continue

        except Exception as e:
            logging.error(f"Failed to send {notification_type} notification: {str(e)}")
            successful = False
            continue

    return successful

def send_notifications(notifications, enabled_notifications, notification_category='collected'):
    """Buffer notifications and send them after a short delay to allow for batching."""
    buffer_notifications(notifications, enabled_notifications, notification_category)

def send_discord_notification(webhook_url, content):
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(webhook_url, json={'content': content})
            response.raise_for_status()
            if attempt > 0:
                logging.info(f"Discord notification sent successfully after {attempt + 1} attempts")
            return True
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                logging.warning(f"Discord notification attempt {attempt + 1} failed: {str(e)}. Retrying...")
                time.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                logging.error(f"Discord notification failed after {MAX_RETRIES} attempts: {str(e)}")
                raise

def send_email_notification(smtp_config, content):
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_config['from_address']
        msg['To'] = smtp_config['to_address']
        msg['Subject'] = "New Media Collected"
        msg.attach(MIMEText(content, 'html'))  # Change 'plain' to 'html'

        with smtplib.SMTP(smtp_config['smtp_server'], smtp_config['smtp_port']) as server:
            server.starttls()
            server.login(smtp_config['smtp_username'], smtp_config['smtp_password'])
            server.send_message(msg)
        logging.info(f"Email notification sent successfully")
    except Exception as e:
        logging.error(f"Failed to send email notification: {str(e)}")

def send_ntfy_notification(host, api_key, priority, topic, content):
    if not priority:
        priority = "low"
    headers={
                "Icon": "https://raw.githubusercontent.com/godver3/cli_debrid/refs/heads/main/static/white-icon-32x32.png",
                "Priority": priority
            }
    if api_key:
        headers["Authorization"]= f"Bearer {api_key}"
    try:
        response = requests.post(f"https://{host}/{topic}",
            data= (content).encode('utf-8'),
            headers=headers)
        response.raise_for_status()
        logging.info(f"NTFY notification sent successfully")
    except Exception as e:
        logging.error(f"Failed to send NTFY notification: {str(e)}")

def send_telegram_notification(bot_token, chat_id, content):
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds
    
    for attempt in range(MAX_RETRIES):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            response = requests.post(url, json={'chat_id': chat_id, 'text': content, 'parse_mode': 'HTML'})
            response.raise_for_status()
            if attempt > 0:
                logging.info(f"Telegram notification sent successfully after {attempt + 1} attempts")
            return True
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                logging.warning(f"Telegram notification attempt {attempt + 1} failed: {str(e)}. Retrying...")
                time.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                logging.error(f"Telegram notification failed after {MAX_RETRIES} attempts: {str(e)}")
                raise

def verify_notification_config(notification_type, config):
    schema = SETTINGS_SCHEMA['Notifications']['schema'][notification_type]
    for key, value in schema.items():
        if key not in config or not config[key]:
            if value.get('default') is None:  # Consider it required if there's no default value
                return False, f"Missing required field: {key}"
    return True, None

def get_enabled_notifications():
    """Get enabled notifications from either the settings route or directly from config."""
    try:
        # Try to use the Flask route first
        from routes.settings_routes import get_enabled_notifications as get_notifications
        enabled_notifications_response = get_notifications()
        logging.debug("Successfully got enabled notifications from Flask route")
        return enabled_notifications_response.get_json()['enabled_notifications']
    except RuntimeError as e:  # Catches "Working outside of application context"
        if "Working outside of application context" in str(e):
            # This is expected during startup, just log at debug level
            logging.debug("Outside Flask context, reading notifications directly from config")
        else:
            # Log other RuntimeErrors as errors
            logging.error(f"Unexpected RuntimeError in get_enabled_notifications: {str(e)}")
            
        # If we're outside Flask context (e.g. during startup), read directly from config
        from settings import load_config
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            if not notification_config or not notification_config.get('enabled', False):
                continue

            # Only include notifications that have the required fields
            if notification_config['type'] == 'Discord':
                if notification_config.get('webhook_url'):
                    enabled_notifications[notification_id] = notification_config
            elif notification_config['type'] == 'Email':
                if all([
                    notification_config.get('smtp_server'),
                    notification_config.get('smtp_port'),
                    notification_config.get('smtp_username'),
                    notification_config.get('smtp_password'),
                    notification_config.get('from_address'),
                    notification_config.get('to_address')
                ]):
                    enabled_notifications[notification_id] = notification_config
            elif notification_config['type'] == 'Telegram':
                if all([
                    notification_config.get('bot_token'),
                    notification_config.get('chat_id')
                ]):
                    enabled_notifications[notification_id] = notification_config
            elif notification_config['type'] == 'NTFY':
                if all([
                    notification_config.get('host'),
                    notification_config.get('topic')
                ]):
                    enabled_notifications[notification_id] = notification_config

        logging.debug(f"Found {len(enabled_notifications)} enabled notifications from config")
        return enabled_notifications

def send_program_stop_notification(message="Program stopped"):
    """Send notification when program stops."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'program_stop')

def send_program_crash_notification(error_message="Program crashed"):
    """Send notification when program crashes."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(error_message, enabled_notifications, 'program_crash')

def send_program_start_notification(message="Program started"):
    """Send notification when program starts."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'program_start')

def send_queue_pause_notification(message="Queue processing paused"):
    """Send notification when queue is paused."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'queue_pause')

def send_queue_resume_notification(message="Queue processing resumed"):
    """Send notification when queue is resumed."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'queue_resume')

def send_queue_start_notification(message="Queue processing started"):
    """Send notification when queue is started."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'queue_start')

def send_queue_stop_notification(message="Queue processing stopped"):
    """Send notification when queue is stopped."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(message, enabled_notifications, 'queue_stop')

def send_upgrade_failed_notification(item_data):
    """Send notification when an upgrade fails."""
    enabled_notifications = get_enabled_notifications()
    _send_notifications(item_data, enabled_notifications, 'upgrade_failed')

def setup_crash_handler():
    """Set up system-wide exception handler for crash notifications."""
    def crash_handler(exctype, value, traceback):
        error_message = f"Program crashed: {exctype.__name__}: {str(value)}"
        send_program_crash_notification(error_message)
        sys.__excepthook__(exctype, value, traceback)  # Call the default handler
    
    sys.excepthook = crash_handler

def register_shutdown_handler():
    """Register handler for graceful shutdown notifications."""
    def shutdown_handler():
        send_program_stop_notification("Program shutting down gracefully")
    
    import atexit
    atexit.register(shutdown_handler)

def register_startup_handler():
    """Register handler for program startup notifications."""
    send_program_start_notification("Program starting up")