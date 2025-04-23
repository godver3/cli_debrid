import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from utilities.settings_schema import SETTINGS_SCHEMA
from collections import defaultdict
from datetime import datetime
import time
from threading import Timer, Lock
import sys
from utilities.settings import get_setting
from database.core import (
    add_db_notification,
    get_db_notifications,
    mark_db_notification_read,
    mark_all_db_notifications_read
)

# Global notification buffer
notification_buffer = []
notification_timer = None
safety_valve_timer = None
buffer_lock = Lock()
BUFFER_TIMEOUT = 10  # seconds to wait before sending notifications
SAFETY_VALVE_TIMEOUT = 60  # seconds maximum to wait before forcing send

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
    
    # Helper function to safely handle sorting of potentially mixed types
    def safe_value(value, default=''):
        return value if value is not None else default
    
    # First pass: organize all items
    for notification in sorted(notifications, key=lambda x: (
        x['title'],
        safe_value(x.get('season_number', '')),
        safe_value(x.get('episode_number', ''))
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
            
            # Safely format season and episode numbers
            try:
                season_num = int(season_number)
                episode_num = int(episode_number)
                item_info['episode'] = f"S{season_num:02d}E{episode_num:02d}"
            except (ValueError, TypeError):
                # If conversion fails, use simple string formatting without padding
                item_info['episode'] = f"S{season_number}E{episode_number}"
            
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
        'upgrade_failed': "❌",
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

    # --- START: Deduplicate notifications within this batch ---
    unique_notifications = []
    seen_keys = set()
    if isinstance(notifications, list): # Ensure it's a list before iterating
        for item in notifications:
            if not isinstance(item, dict): # Skip non-dict items
                logging.warning(f"Skipping non-dictionary item during notification deduplication: {item}")
                continue
            # Create a unique key based on essential identifying information and the target state
            media_type = item.get('type', 'movie')
            key_parts = [
                item.get('title'),
                item.get('year'),
                media_type,
                item.get('version', '').strip('*'),
                item.get('new_state'), # Include the state in the key
                item.get('is_upgrade', False)
            ]
            if media_type == 'episode':
                key_parts.extend([item.get('season_number'), item.get('episode_number')])
            
            key = tuple(key_parts)

            if key not in seen_keys:
                seen_keys.add(key)
                unique_notifications.append(item)
            else:
                 logging.debug(f"Skipping duplicate notification item: {key}")
    else:
        # If notifications is not a list (e.g., single system message), keep it as is
        unique_notifications = notifications

    # --- END: Deduplicate notifications ---

    # --- START EDIT: Use the deduplicated list from now on ---
    if not unique_notifications:
         return "" # Return empty string if no unique notifications left
    # --- END EDIT ---

    def format_state_suffix(state, is_upgrade=False):
        """Return the appropriate suffix based on state"""
        if state == 'Collected' and is_upgrade:
            return f"→ Upgraded"
        else:
            return f"→ {state}"

    def format_title(item):
        """Format the title with appropriate prefix and formatting."""
        from utilities.settings import get_setting
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
                try:
                    season = int(season)
                    episode = int(episode)
                except (ValueError, TypeError):
                    # If conversion fails, use string formatting instead
                    return f"    S{season}E{episode}{' [' + item.get('version', '') + ']' if item.get('version') else ''}"
                
                version = item.get('version', '')
                version_str = f" [{version}]" if version else ""
                return f"    S{season:02d}E{episode:02d}{version_str}"
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid season/episode format: {str(e)} - S:{season} E:{episode}")
        return None

    # Group items by show/movie
    grouped_items = {}
    for item in unique_notifications:
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
            # Convert season and episode numbers to integers for sorting, with fallback handling
            def safe_convert_to_int(value, default=0):
                try:
                    return int(value) if value is not None else default
                except (ValueError, TypeError):
                    return default
            
            sorted_items = sorted(items, key=lambda x: (
                safe_convert_to_int(x.get('season_number', 0)), 
                safe_convert_to_int(x.get('episode_number', 0))
            ))
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

def get_all_notifications():
    """Get all notifications from the database."""
    notifications_list, error = get_db_notifications(sort_order='DESC') # Get newest first
    
    if error:
        logging.error(f"Failed to get notifications from database: {error}")
        # Return error structure similar to original file-based one
        return {"notifications": [], "error": f"Database error: {error}"}, 500
    
    # Format data as expected by the API endpoint
    result_data = {"notifications": notifications_list}
    return result_data, 200 # Return data and OK status

def mark_single_notification_read(notification_id):
    """Mark a single notification as read by its ID in the database."""
    success, found, error = mark_db_notification_read(notification_id)
    
    if not success:
        logging.error(f"Failed to mark notification {notification_id} as read: {error}")
        return {"error": error or "Failed to update notification status"}, 500
    
    if not found:
        return {"error": "Notification not found"}, 404
        
    return {"success": True}, 200

def mark_all_notifications_read():
    """Mark all notifications as read in the database."""
    success, count_updated, error = mark_all_db_notifications_read()
    
    if not success:
        logging.error(f"Failed to mark all notifications as read: {error}")
        return {"error": error or "Failed to update notification statuses"}, 500
        
    if count_updated == 0:
        return {"success": True, "message": "No unread notifications found"}, 200
        
    return {"success": True, "message": f"Marked {count_updated} notifications as read"}, 200

def store_notification(title, message, notification_type='info', link=None):
    """Store a notification in the database."""
    # Check if notifications are globally disabled
    notifications_disabled = get_setting('Notifications', 'disable_all', False)
    is_read = notifications_disabled # Mark as read immediately if globally disabled

    success, error = add_db_notification(
        title=title,
        message=message,
        notification_type=notification_type,
        link=link,
        is_read=is_read
    )

    if not success:
        logging.error(f"Failed to store notification in database: {error}")
        # Optionally, decide if this failure should prevent sending (return False)
        # or just be logged (return True or None depending on desired behavior)
        return False # Indicate failure to store

    return True # Indicate success storing

def _send_notifications(notifications, enabled_notifications, notification_category=None):
    # Attempt to store notifications in the database first
    storage_successful = True
    try:
        # Handle system operation notifications
        if notification_category in ['program_crash', 'program_stop', 'program_start',
                                   'queue_pause', 'queue_resume', 'queue_start', 'queue_stop',
                                   'scraping_error', 'content_error', 'database_error']:

            # --- RESTORED DICTIONARIES ---
            title = {
                'program_crash': "Program Crashed",
                'program_stop': "Program Stopped",
                'program_start': "Program Started",
                'queue_pause': "Queue Paused",
                'queue_resume': "Queue Resumed",
                'queue_start': "Queue Started",
                'queue_stop': "Queue Stopped",
                'scraping_error': "Scraping Error",
                'content_error': "Content Error",
                'database_error': "Database Error"
            }.get(notification_category, "System Notification") # Added default

            notif_type = {
                'program_crash': 'error',
                'program_stop': 'info',
                'program_start': 'success',
                'queue_pause': 'warning',
                'queue_resume': 'success',
                'queue_start': 'success',
                'queue_stop': 'info',
                'scraping_error': 'error',
                'content_error': 'error',
                'database_error': 'error'
            }.get(notification_category, 'info') # Added default

            # Message handling was slightly different, ensure it's correct
            if isinstance(notifications, str):
                 message = notifications # Use the direct message if it's a string
            else:
                # Fallback messages if 'notifications' wasn't a simple string
                message = {
                    'program_crash': "Program crashed unexpectedly",
                    'program_stop': "Program has been stopped",
                    'program_start': "Program has been started",
                    # 'queue_pause': notifications, # This was problematic if notifications wasn't a string
                    'queue_resume': "Queue processing has been resumed",
                    'queue_start': "Queue processing has started",
                    'queue_stop': "Queue processing has stopped",
                    'scraping_error': "Error occurred during scraping",
                    'content_error': "Error processing content",
                    'database_error': "Database operation failed"
                }.get(notification_category, "System event occurred.") # Default/fallback
                # Special case for queue_pause where the message might be specific
                if notification_category == 'queue_pause' and not isinstance(notifications, str):
                     message = "Queue processing paused" # Default pause message if specific reason not passed as string

            # --- END RESTORED DICTIONARIES ---

            if not store_notification(title, message, notif_type):
                storage_successful = False

        elif notification_category == 'upgrade_failed':
            if isinstance(notifications, dict):
                title = "Upgrade Failed"
                message = f"Failed to upgrade {notifications.get('title', 'Unknown')} ({notifications.get('year', '')}): {notifications.get('reason', 'Unknown reason')}"
                if not store_notification(title, message, 'error', link="/queues"):
                    storage_successful = False
            else:
                 logging.warning(f"Received upgrade_failed notification with non-dict data: {notifications}")
                 storage_successful = False # Cannot process this

        else: # Content notifications
             if not isinstance(notifications, list):
                 logging.error(f"Expected a list for content notifications, but got {type(notifications)}. Skipping storage.")
                 storage_successful = False
             else:
                for notification in notifications:
                    if not isinstance(notification, dict):
                        logging.warning(f"Skipping non-dict item in content notification list: {notification}")
                        continue

                    # --- RESTORED LOGIC ---
                    title_base = notification.get('title', '')
                    year = notification.get('year', '')
                    version = notification.get('version', '').strip('*')
                    media_type = notification.get('type', 'movie')
                    new_state = notification.get('new_state', '')

                    base_message = f"{title_base} ({year})"
                    if media_type == 'episode':
                        season_num = notification.get('season_number', '??')
                        episode_num = notification.get('episode_number', '??')
                        # Ensure formatting even if numbers aren't integers
                        try: season_formatted = f"{int(season_num):02d}"
                        except: season_formatted = str(season_num)
                        try: episode_formatted = f"{int(episode_num):02d}"
                        except: episode_formatted = str(episode_num)
                        base_message += f" S{season_formatted}E{episode_formatted}"

                    if version:
                        base_message += f" [{version}]"

                    if new_state == 'Downloading':
                        notification_title = "Downloading Content"
                        final_message = f"Started downloading {base_message}"
                        notif_type = 'info'
                    elif new_state == 'Checking':
                        notification_title = "Checking Content"
                        final_message = f"Checking {base_message}"
                        notif_type = 'info'
                    elif new_state == 'Upgrading':
                        notification_title = "Upgrading Content"
                        final_message = f"Upgrading {base_message}"
                        if notification.get('upgrading_from'):
                             final_message += f"\nUpgrading from: {notification['upgrading_from']}"
                        notif_type = 'info'
                    elif new_state == 'Upgraded':
                        notification_title = "Content Upgraded"
                        final_message = f"Successfully upgraded {base_message}"
                        notif_type = 'success'
                    else: # Default Collected
                        notification_title = "New Content Available"
                        if notification.get('is_upgrade'):
                            final_message = f"Upgraded and collected {base_message}"
                        else:
                            final_message = f"Successfully collected {base_message}"
                        notif_type = 'success'

                    # Add source information if available
                    source_info = []
                    if notification.get('content_source'):
                        source_info.append(f"Source: {notification['content_source']}")
                    if notification.get('content_source_detail'):
                        source_info.append(f"Requested by: {notification['content_source_detail']}")
                    if notification.get('filled_by_file'):
                         source_info.append(f"File: {notification['filled_by_file']}")
                    if source_info:
                        final_message += "\n" + "\n".join(source_info)
                    # --- END RESTORED LOGIC ---

                    if not store_notification(notification_title, final_message, notif_type, link="/queues"):
                        storage_successful = False
                        # break # Optional: stop on first failure

    except Exception as e:
        logging.error(f"Error during notification storage pre-processing: {str(e)}", exc_info=True)
        storage_successful = False

    # Only attempt to send notifications if storage was successful
    # (Or adjust this logic if sending should happen even if DB store fails)
    if not storage_successful:
        logging.warning("Skipping notification sending because database storage failed.")
        return False # Indicate overall failure

    logging.debug(f"Attempting to send external notifications for category: {notification_category}")
    send_successful = True
    processed_discord = False # Flag to check if we even attempted Discord

    for notification_id, notification_config in enabled_notifications.items():
        logging.debug(f"Processing notification target ID: {notification_id}")

        if not notification_config.get('enabled', False):
            logging.debug(f"Target {notification_id} is NOT enabled.")
            continue
        logging.debug(f"Target {notification_id} IS enabled.")

        notify_on = notification_config.get('notify_on', {})
        category_enabled = notify_on.get(notification_category, True) # Default to True if key missing
        if not category_enabled:
            logging.debug(f"Target {notification_id} has category '{notification_category}' DISABLED.")
            continue
        logging.debug(f"Target {notification_id} has category '{notification_category}' ENABLED.")

        notification_type = notification_config.get('type')
        logging.debug(f"Target {notification_id} type is '{notification_type}'.")

        content = "" # Initialize content
        try:
            content = format_notification_content(notifications, notification_type, notification_category)
            logging.debug(f"Formatted content for {notification_id} ({notification_type}): {content[:100]}...")
        except Exception as e:
            logging.error(f"Failed to format notification content for {notification_type} ({notification_id}): {str(e)}")
            send_successful = False
            continue

        send_result = None # Variable to store result from sender function
        try:
            if notification_type == 'Discord':
                processed_discord = True # Mark that we tried Discord
                webhook_url = notification_config.get('webhook_url')
                if not webhook_url:
                    logging.warning(f"Skipping Discord notification ({notification_id}): webhook URL is empty")
                    continue

                logging.info(f"--> Attempting to send Discord notification for {notification_id}...")
                send_result = send_discord_notification(webhook_url, content) # Store the result
                if send_result:
                     logging.info(f"<-- Discord notification for {notification_id} SUCCEEDED.")
                else:
                     logging.warning(f"<-- Discord notification for {notification_id} FAILED after retries.")

            # ... other types ...

            # If a sender function returned False, update overall status
            if send_result == False: # Explicitly check for False
                 send_successful = False

        except Exception as e:
            logging.error(f"Failed to send {notification_type} notification ({notification_id}): {str(e)}")
            send_successful = False
            continue

    if not processed_discord:
         logging.debug("No Discord notification target was processed (check enabled status, category filter, and type).")

    logging.debug(f"Finished sending external notifications for category '{notification_category}'. Overall success: {send_successful}")
    return send_successful

def send_notifications(notifications, enabled_notifications, notification_category='collected'):
    # This function remains the same - it just calls buffer_notifications
    buffer_notifications(notifications, enabled_notifications, notification_category)

def send_discord_notification(webhook_url, content):
    MAX_RETRIES = 3
    RETRY_DELAY = 1

    for attempt in range(MAX_RETRIES):
        try:
            logging.debug(f"Discord POST attempt {attempt + 1} to {webhook_url}")
            response = requests.post(webhook_url, json={'content': content}, timeout=15)
            logging.debug(f"Discord POST attempt {attempt + 1} status: {response.status_code}, Response: {response.text[:200]}")

            response.raise_for_status()

            # If successful:
            logging.info(f"Discord POST attempt {attempt + 1} successful.")
            return True

        except requests.exceptions.Timeout:
             logging.warning(f"Discord POST attempt {attempt + 1} timed out.")
             if attempt < MAX_RETRIES - 1:
                 time.sleep(RETRY_DELAY * (attempt + 1))
             else:
                 logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts due to timeout.")
        except requests.exceptions.RequestException as e:
            # Includes HTTP errors from raise_for_status
            logging.warning(f"Discord POST attempt {attempt + 1} failed: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts: {str(e)}")

    # If loop finishes without returning True
    return False

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
        from utilities.settings import load_config
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
    
    # Filter out notifications where 'upgrading' is disabled
    filtered_notifications = {}
    for notification_id, notification_config in enabled_notifications.items():
        if not notification_config.get('enabled', False):
            continue
            
        notify_on = notification_config.get('notify_on', {})
        
        # Skip if 'upgrading' notifications are disabled
        if 'upgrading' in notify_on and not notify_on['upgrading']:
            logging.debug(f"Skipping {notification_id} notification: upgrade_failed notifications are disabled (via upgrading setting)")
            continue
            
        # Add to filtered list
        filtered_notifications[notification_id] = notification_config
        
    _send_notifications(item_data, filtered_notifications, 'upgrade_failed')

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