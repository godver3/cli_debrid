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
import math # Add math import for ceiling division
from queues.config_manager import get_overseerr_instances # Added import

# Global notification buffer
notification_buffer = []
notification_timer = None
safety_valve_timer = None
buffer_lock = Lock()
BUFFER_TIMEOUT = 10  # seconds to wait before sending notifications
SAFETY_VALVE_TIMEOUT = 60  # seconds maximum to wait before forcing send

# --- Overseerr Scan Scheduling ---
OVERSEERR_SCAN_DELAY = 30  # seconds
overseerr_scan_schedulers = {} # Stores { 'overseerr_instance_id': threading.Timer }
overseerr_job_id_cache = {} # Stores { 'overseerr_instance_id': 'job_id_for_scan' }
# --- End Overseerr Scan Scheduling ---

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
        'blacklisted': "🚫" # New emoji for blacklisted
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
                logging.warning(f"Notifications: Skipping non-dictionary item during deduplication: {item}")
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
        # If notifications is not a list (e.g., single system message), keep it as is
        unique_notifications = notifications

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
        elif new_state == 'Blacklisted': # New case for Blacklisted
            prefix = EMOJIS['blacklisted']
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
                    return f"    S{season}E{episode}{' [' + item.get('version', '').strip('*') + ']' if item.get('version') else ''}"

                version = item.get('version', '').strip('*')
                version_str = f" [{version}]" if version else ""
                return f"    S{season:02d}E{episode:02d}{version_str}"
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid season/episode format: {str(e)} - S:{season} E:{episode}")
        return None

    # Group items by show/movie
    grouped_items = {}
    for item in unique_notifications:
        # --- START EDIT: Check for Checking state during an upgrade ---
        # If the state is 'Checking' but 'upgrading_from' is present, treat it as 'Upgrading' for formatting.
        original_state = item.get('new_state', '') # Keep original state for logging
        effective_state = original_state
        if effective_state == 'Checking' and item.get('upgrading_from'):
            effective_state = 'Upgrading'
            # Optionally, ensure is_upgrade is also True if not already set, though it should be
            # item['is_upgrade'] = True
        # --- END EDIT ---

        # Group by title, type, year, and the *effective* state for better batching
        # --- EDIT: Add version to the grouping key ---
        version_key_part = item.get('version', '').strip('*') # Get version for key explicitly
        key = (item.get('title'), item.get('type'), item.get('year'), effective_state, item.get('is_upgrade', False), version_key_part)
        # --- END EDIT ---
        if key not in grouped_items:
            grouped_items[key] = []
        # Store the original item, but we'll use the effective_state when formatting
        grouped_items[key].append(item)
        # More detailed log for adding item to group

    content = []

    # Process each group
    # --- EDIT: Update the key unpacking to include version ---
    for (title, type_, year, state, is_upgrade, version), items in sorted(grouped_items.items()):
    # --- END EDIT ---
        # Log details about the group being processed
        group_key_for_log = (title, type_, year, state, is_upgrade, version)
        # Create a representative item for the group
        group_item = items[0].copy()

        # --- EDIT: Use the effective state for formatting ---
        # Ensure the representative item uses the effective state determined during grouping
        group_item['new_state'] = state # 'state' here comes from the group key (effective_state)
        # --- END EDIT ---

        # Add the title line only once per group using the (potentially modified) group_item
        formatted_title_line = format_title(group_item) # Get the formatted title
        content.append(formatted_title_line) # Add it

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

            # --- START: Truncate episode notifications ---
            from utilities.settings import get_setting # Import for the new setting
            truncate_episodes = get_setting('Debug', 'truncate_episode_notifications', False)
            
            if truncate_episodes and len(sorted_items) > 1:
                # Show the first episode
                first_item = sorted_items[0]
                effective_episode_state = first_item.get('new_state', '')
                if effective_episode_state == 'Checking' and first_item.get('upgrading_from'):
                    effective_episode_state = 'Upgrading'
                
                episode_line = format_episode(first_item)
                if episode_line:
                    content.append(f"{episode_line} {format_state_suffix(effective_episode_state, first_item.get('is_upgrade', False))}")

                # Add a summary for the rest
                num_other_episodes = len(sorted_items) - 1
                content.append(f"    ...and {num_other_episodes} other episode(s).")
            else:
                # Original behavior if not truncating or only one episode
                for item in sorted_items:
                    # --- EDIT: Use the effective state for episode suffix ---
                    effective_episode_state = item.get('new_state', '')
                    if effective_episode_state == 'Checking' and item.get('upgrading_from'):
                        effective_episode_state = 'Upgrading'
                    
                    episode_line = format_episode(item)
                    if episode_line:
                        # Pass the effective state to format_state_suffix
                        content.append(f"{episode_line} {format_state_suffix(effective_episode_state, item.get('is_upgrade', False))}")
                    # --- END EDIT ---
            # --- END: Truncate episode notifications ---
        else:
            # For movies, just add the state suffix (using the effective state) to the title line
            state_suffix = format_state_suffix(state, is_upgrade) # Get suffix
            content[-1] = f"{content[-1]} {state_suffix}" # Append suffix

    # Join with single newlines between items
    final_content = "\n".join(content)
    logging.debug(f"Notifications: Final formatted notification:\n---\n{final_content}\n---") # Log final content
    return final_content

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

    # Broadcast the new notification to connected clients
    try:
        from routes.base_routes import broadcast_notification
        notification_data = {
            'type': 'new_notification',
            'notification': {
                'title': title,
                'message': message,
                'notification_type': notification_type,
                'link': link,
                'timestamp': datetime.now().isoformat(),
                'is_read': is_read
            }
        }
        broadcast_notification(notification_data)
    except Exception as e:
        logging.debug(f"Failed to broadcast notification: {e}")

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
                if not store_notification(title, message, 'error', link="/debug/torrent_tracking"):
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
                    elif new_state == 'Blacklisted': # New case for Blacklisted
                        notification_title = "Item Blacklisted"
                        final_message = f"Item has been blacklisted: {base_message}"
                        notif_type = 'warning' # Or 'info'
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

                    if not store_notification(notification_title, final_message, notif_type, link="/debug/torrent_tracking"):
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
    processed_email = False # Flag to check if we even attempted Email
    # Add flags for other types if needed

    # --- START: Overseerr Scan Scheduling Trigger ---
    trigger_scan_check = False
    if notification_category in ['collected', 'upgrading']:
        if notifications: # Ensure there's at least one notification
            trigger_scan_check = True
    elif notification_category == 'state_change' and isinstance(notifications, list):
        for item_data in notifications:
            if isinstance(item_data, dict):
                current_state = item_data.get('new_state')
                if current_state == 'Collected' or current_state == 'Upgraded':
                    trigger_scan_check = True
                    break # One item is enough to trigger the scan check for all instances
    
    if trigger_scan_check:
        handle_overseerr_scan_scheduling()
    # --- END: Overseerr Scan Scheduling Trigger ---

    # --- START: MODIFICATION TO PREVENT DUPLICATE 'Collected'/'Upgraded' IN 'state_change' BATCH ---
    if notification_category == 'state_change' and isinstance(notifications, list):
        original_count = len(notifications)
        temp_notifications = []
        for item_in_batch in notifications:
            if not isinstance(item_in_batch, dict):
                temp_notifications.append(item_in_batch) # Keep non-dict items
                continue

            item_state = item_in_batch.get('new_state')
            
            # If an item's state is 'Collected' or 'Upgraded',
            # it's assumed to be handled by the dedicated 'collected'/'upgrading' notification flow.
            # We exclude it from this generic 'state_change' batch to prevent duplication.
            if item_state == 'Collected' or item_state == 'Upgraded':
                continue
            temp_notifications.append(item_in_batch)
        
        notifications = temp_notifications # Use the filtered list
        if original_count > 0 and not notifications:
            pass
            # No need to return early, subsequent logic will handle an empty 'notifications' list
            # by eventually producing no content to send.
    # --- END: MODIFICATION ---

    for notification_id, notification_config in enabled_notifications.items():

        if not notification_config.get('enabled', False):
            continue
        logging.debug(f"Target {notification_id} IS enabled.")

        notify_on = notification_config.get('notify_on', {})
        # --- Primary Batch Category Check ---
        # Check if the target is enabled for the overall category of this notification batch
        category_enabled = notify_on.get(notification_category, True) # Default to True if key missing
        if not category_enabled:
            continue

        # --- Item-Level Filtering (New Logic) ---
        content_input = notifications # Default to using the original batch
        if isinstance(notifications, list):
            # If the batch is a list of items (e.g., content notifications), filter it further
            filtered_items = []
            for item in notifications:
                if not isinstance(item, dict): # Skip non-dict items just in case
                    logging.warning(f"Skipping non-dictionary item during item-level filtering: {item}")
                    continue

                # Determine the specific category for *this* item
                item_category = 'collected' # Default
                state = item.get('new_state')
                is_upgrade = item.get('is_upgrade', False)

                if state in ['Upgrading', 'Upgraded'] or (state == 'Collected' and is_upgrade):
                    item_category = 'upgrading'
                elif state == 'Collected' and not is_upgrade:
                    item_category = 'collected'
                elif state == 'Downloading':
                    item_category = 'downloading'
                elif state == 'Checking':
                    item_category = 'checking'
                elif state == 'Blacklisted': # New case for Blacklisted
                    item_category = 'blacklisted'
                # Add elif for other states if they map to specific notify_on keys

                # Check if the target is enabled for this specific item's category
                item_category_enabled = notify_on.get(item_category, True) # Default to True if key missing

                if item_category_enabled:
                    filtered_items.append(item)

            if not filtered_items:
                continue # Skip this target if no relevant items remain

            content_input = filtered_items # Use the filtered list for formatting

        # --- Content Source Filtering ---
        if isinstance(content_input, list):
            # Get the enabled content sources setting
            from utilities.settings import get_setting
            notifications_config = get_setting('Notifications', None, {})
            enabled_content_sources_str = notifications_config.get('General', {}).get('enabled_content_sources', '')
            
            # Initialize enabled_sources as empty set
            enabled_sources = set()
            
            if enabled_content_sources_str:
                logging.debug(f"Content source filtering setting found: '{enabled_content_sources_str}'")
                # Parse the comma-separated list and create a set for efficient lookup
                enabled_sources = {source.strip() for source in enabled_content_sources_str.split(',') if source.strip()}
                logging.debug(f"Parsed enabled sources: {enabled_sources}")
            else:
                logging.debug("No content source filtering setting found - all sources will be included")
            
            if enabled_sources:
                    logging.debug(f"Content source filtering enabled for {notification_id}. Allowed sources: {enabled_sources}")
                    
                    # Get content source mapping for better logging and potential display name matching
                    try:
                        from queues.config_manager import get_enabled_content_sources
                        content_source_mapping = {}
                        for source in get_enabled_content_sources():
                            content_source_mapping[source['id']] = source['display_name']
                            # Also allow matching by display name (case-insensitive)
                            content_source_mapping[source['display_name'].lower()] = source['id']
                    except Exception as e:
                        logging.warning(f"Could not load content source mapping for filtering: {e}")
                        content_source_mapping = {}
                    
                    # Filter items based on content source
                    source_filtered_items = []
                    filtered_out_count = 0
                    
                    for item in content_input:
                        if not isinstance(item, dict):
                            source_filtered_items.append(item)  # Keep non-dict items
                            continue
                            
                        item_content_source = item.get('content_source', '')
                        
                        # If content source is missing, try to look it up from the database
                        if not item_content_source and item.get('id'):
                            try:
                                from database import get_db_connection
                                conn = get_db_connection()
                                cursor = conn.cursor()
                                cursor.execute('SELECT content_source FROM media_items WHERE id = ?', (item['id'],))
                                result = cursor.fetchone()
                                if result and result[0]:
                                    item_content_source = result[0]
                                    logging.debug(f"Retrieved content_source '{item_content_source}' from database for item {item['id']}")
                                conn.close()
                            except Exception as db_error:
                                logging.warning(f"Failed to look up content_source from database for item {item.get('id')}: {db_error}")
                        
                        # Check if the item's content source is in the enabled list
                        # We check both the direct match and potential display name match
                        is_allowed = False
                        if not item_content_source:
                            is_allowed = True  # Items without content source are included
                        else:
                            # Direct ID match
                            if item_content_source in enabled_sources:
                                is_allowed = True
                            else:
                                # Check if it's a display name match
                                display_name_lower = item_content_source.lower()
                                if display_name_lower in content_source_mapping:
                                    mapped_id = content_source_mapping[display_name_lower]
                                    if mapped_id in enabled_sources:
                                        is_allowed = True
                        
                        if is_allowed:
                            source_filtered_items.append(item)
                        else:
                            filtered_out_count += 1
                            display_name = content_source_mapping.get(item_content_source, item_content_source)
                            logging.debug(f"Filtered out notification for {item.get('title', 'Unknown')} from source '{display_name}' (not in allowed list)")
                    
                    content_input = source_filtered_items
                    
                    if filtered_out_count > 0:
                        logging.info(f"Content source filtering: {filtered_out_count} items filtered out for {notification_id}")
                    
                    if not source_filtered_items:
                        logging.debug(f"No items remain after content source filtering for {notification_id}")
                        continue  # Skip this target if no items remain after content source filtering

        # --- End Content Source Filtering ---
        # --- End Item-Level Filtering ---


        notification_type = notification_config.get('type')

        content = "" # Initialize content
        try:
            # Pass the potentially filtered list (content_input) to the formatter
            content = format_notification_content(content_input, notification_type, notification_category)
            if not content: # Handle case where formatting results in empty string (e.g., after deduplication)
                 continue
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

                send_result = send_discord_notification(webhook_url, content) # Store the result
                if not send_result:
                     logging.warning(f"<-- Discord notification for {notification_id} FAILED after retries.")

            elif notification_type == 'Email':
                processed_email = True # Mark that we tried Email
                smtp_config = {
                    'from_address': notification_config.get('from_address'),
                    'to_address': notification_config.get('to_address'),
                    'smtp_server': notification_config.get('smtp_server'),
                    'smtp_port': notification_config.get('smtp_port'),
                    'smtp_username': notification_config.get('smtp_username'),
                    'smtp_password': notification_config.get('smtp_password')
                }
                # Basic validation - only check required fields
                required_fields = ['from_address', 'to_address', 'smtp_server', 'smtp_port']
                if not all(smtp_config.get(field) for field in required_fields):
                     logging.warning(f"Skipping Email notification ({notification_id}): Missing required SMTP configuration fields.")
                     continue

                # Pass notification_category here
                send_result = send_email_notification(smtp_config, content, notification_category)
                if not send_result:
                     logging.warning(f"<-- Email notification for {notification_id} FAILED after retries.")

            elif notification_type == 'Telegram':
                # Add a flag for processed_telegram if you are tracking it
                # processed_telegram = True 
                bot_token = notification_config.get('bot_token')
                chat_id = notification_config.get('chat_id')
                if not bot_token or not chat_id:
                    logging.warning(f"Skipping Telegram notification ({notification_id}): bot_token or chat_id is empty")
                    continue

                try:
                    send_result = send_telegram_notification(bot_token, chat_id, content)
                    if not send_result:
                         logging.warning(f"<-- Telegram notification for {notification_id} FAILED after retries.")
                except Exception as e: # send_telegram_notification can raise exceptions
                    logging.error(f"<-- Telegram notification for {notification_id} FAILED with exception: {str(e)}")
                    send_result = False

            elif notification_type == 'NTFY':
                # Add a flag for processed_ntfy if you are tracking it
                # processed_ntfy = True
                host = notification_config.get('host')
                api_key = notification_config.get('api_key') # Optional
                priority = notification_config.get('priority') # Optional
                topic = notification_config.get('topic')

                if not host or not topic:
                    logging.warning(f"Skipping NTFY notification ({notification_id}): host or topic is empty")
                    continue

                try:
                    # send_ntfy_notification doesn't explicitly return True/False
                    # but logs success/failure. We'll assume success if no exception.
                    send_ntfy_notification(host, api_key, priority, topic, content)
                    send_result = True # Assume success if no exception
                except Exception as e:
                    logging.error(f"<-- NTFY notification for {notification_id} FAILED with exception: {str(e)}")
                    send_result = False

            # ... other types ...

            # If a sender function returned False, update overall status
            if send_result == False: # Explicitly check for False
                 send_successful = False

        except Exception as e:
            logging.error(f"Failed to send {notification_type} notification ({notification_id}): {str(e)}")
            send_successful = False
            continue

    return send_successful

def send_notifications(notifications, enabled_notifications, notification_category='collected'):
    # This function remains the same - it just calls buffer_notifications
    buffer_notifications(notifications, enabled_notifications, notification_category)

def send_discord_notification(webhook_url, content):
    MAX_RETRIES = 3
    RETRY_DELAY = 1 # seconds between retries for the *original* message
    CHUNK_SEND_DELAY = 0.5 # seconds between sending *chunks* if splitting occurs

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(webhook_url, json={'content': content}, timeout=15)

            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            # If successful:
            return True # Successfully sent the original message

        except requests.exceptions.Timeout:
             logging.warning(f"Discord POST attempt {attempt + 1} timed out.")
             if attempt < MAX_RETRIES - 1:
                 time.sleep(RETRY_DELAY * (attempt + 1)) # Exponential backoff
             else:
                 logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts due to timeout.")
                 # No splitting on timeout, just fail

        except requests.exceptions.RequestException as e:
            logging.warning(f"Discord POST attempt {attempt + 1} failed: {str(e)}")

            # Check if we have a response and status code to analyze
            if e.response is not None:
                status_code = e.response.status_code
                logging.warning(f"Discord POST attempt {attempt + 1} received status code: {status_code}")

                # --- START: Splitting Logic ---
                # Check for Bad Request (400) which often indicates payload issues (size, formatting)
                # Discord API might use 400 for various issues, but size is a common one for large posts.
                if status_code == 400 and attempt == 0: # Only try splitting on the first attempt failure with 400
                    logging.warning(f"Discord POST failed with 400, potentially due to payload size. Attempting to split content.")
                    try:
                        # Call the helper function to send in chunks
                        split_success = _send_discord_chunks(webhook_url, content, CHUNK_SEND_DELAY)
                        if split_success:
                            return True # Return True as the content was eventually sent
                        else:
                            logging.error("Failed to send Discord notification content even after splitting into chunks.")
                            return False # Return False as splitting also failed
                    except Exception as split_err:
                        logging.error(f"An error occurred during the splitting/chunk sending process: {split_err}")
                        return False # Return False due to error during splitting attempt
                # --- END: Splitting Logic ---

                # Handle Rate Limiting (429)
                elif status_code == 429:
                    logging.warning(f"Discord rate limit hit (429). Retrying after delay...")
                    # Respect Retry-After header if present
                    retry_after = e.response.headers.get('Retry-After')
                    delay = RETRY_DELAY * (attempt + 1) # Default backoff
                    if retry_after:
                        try:
                            # Discord returns Retry-After in seconds (float or int)
                            delay = max(float(retry_after), delay) # Use longer delay if Retry-After is bigger
                        except ValueError:
                            logging.warning(f"Could not parse Retry-After header value: {retry_after}. Using default backoff.")

                    if attempt < MAX_RETRIES - 1:
                         time.sleep(delay)
                         continue # Continue to the next retry attempt
                    else:
                         logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts due to rate limiting.")
                         # Stop retrying after max attempts for rate limit

                # Handle other HTTP errors (continue retry loop for potentially transient server issues)
                else:
                    logging.warning(f"Received unexpected status code {status_code}. Retrying...")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY * (attempt + 1))
                        continue # Continue to the next retry attempt
                    else:
                        logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts with final status code {status_code}.")

            # Handle non-HTTP errors (e.g., network connection issues)
            else:
                logging.warning("Discord POST failed without a response object (e.g., network error). Retrying...")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue # Continue to the next retry attempt
                else:
                    logging.error(f"Discord notification FAILED after {MAX_RETRIES} attempts due to network/connection errors.")

    # If loop finishes without returning True (or calling splitting logic which returns)
    logging.error(f"Discord notification ultimately FAILED after all retries/attempts for the original message.")
    return False

def _send_discord_chunks(webhook_url, original_content, delay_between_chunks, max_lines_per_chunk=15):
    """Splits content by lines and sends it in smaller chunks."""
    lines = original_content.strip().split('\n')
    total_lines = len(lines)
    num_chunks = math.ceil(total_lines / max_lines_per_chunk)
    
    all_chunks_sent = True
    for i in range(num_chunks):
        start_index = i * max_lines_per_chunk
        end_index = start_index + max_lines_per_chunk
        chunk_lines = lines[start_index:end_index]
        chunk_content = "\n".join(chunk_lines)
        
        if not chunk_content:
            continue
                    
        try:
            # Use a simple, single attempt for each chunk for now. Could add retries here too if needed.
            response = requests.post(webhook_url, json={'content': chunk_content}, timeout=10)
            response.raise_for_status() # Check for errors on chunk send
            
            # Add delay before sending the next chunk (if not the last one)
            if i < num_chunks - 1:
                time.sleep(delay_between_chunks)
                
        except requests.exceptions.RequestException as chunk_e:
            logging.error(f"Failed to send chunk {i+1}/{num_chunks}: {str(chunk_e)}")
            # Check for response in chunk error
            if chunk_e.response is not None:
                logging.error(f"Chunk send failed with status: {chunk_e.response.status_code}, response: {chunk_e.response.text[:200]}")
            all_chunks_sent = False
            break # Stop sending remaining chunks if one fails
        except Exception as general_e:
             logging.error(f"An unexpected error occurred sending chunk {i+1}/{num_chunks}: {str(general_e)}")
             all_chunks_sent = False
             break # Stop on unexpected error
             
    return all_chunks_sent

def send_email_notification(smtp_config, content, notification_category):
    # Determine subject based on category
    # --- START: Subject Map Consolidation ---
    # Use descriptive titles from the _send_notifications function where appropriate
    # and add specific ones for system events.
    subject_map = {
        'program_crash': "Program Crashed",
        'program_stop': "Program Stopped",
        'program_start': "Program Started",
        'queue_pause': "Queue Paused",
        'queue_resume': "Queue Resumed", # Corrected: was "Queue Resumed"
        'queue_start': "Queue Started",
        'queue_stop': "Queue Stopped",
        'upgrade_failed': "Upgrade Failed",
        'collected': "Media Notification", # Generic for various content states
        'downloading': "Downloading Content", # Using title from storage logic
        'checking': "Checking Content",     # Using title from storage logic
        'upgrading': "Upgrading Content",   # Using title from storage logic
        'upgraded': "Content Upgraded",      # Using title from storage logic
        'scraping_error': "Scraping Error",
        'content_error': "Content Error",
        'database_error': "Database Error",
        'blacklisted': "Item Blacklisted" # New subject for blacklisted
        # Add other categories as needed
    }
    # Default subject if category not in map or None
    subject = subject_map.get(notification_category, "Media Notification") # Keep default
    # --- END: Subject Map Consolidation ---

    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_config['from_address']
        msg['To'] = smtp_config['to_address']
        msg['Subject'] = subject # Use the dynamic subject
        # Replace newlines with HTML line breaks for email formatting
        html_content = content.replace('\n', '<br>\n')
        msg.attach(MIMEText(html_content, 'html'))

        # Use context manager for SMTP connection
        with smtplib.SMTP(smtp_config['smtp_server'], smtp_config['smtp_port'], timeout=15) as server: # Added timeout
            # Check if encryption is needed (common practice)
            # This assumes STARTTLS; adjust if SSL/TLS is needed directly
            server.ehlo() # Identify ourselves to the server
            if server.has_extn('starttls'):
                logging.debug("Starting TLS for email connection.")
                server.starttls()
                server.ehlo() # Re-identify after starting TLS

            # Login only if username/password are provided
            if smtp_config.get('smtp_username') and smtp_config.get('smtp_password'):
                try:
                    server.login(smtp_config['smtp_username'], smtp_config['smtp_password'])
                except smtplib.SMTPAuthenticationError as auth_err:
                    logging.error(f"SMTP Authentication failed: {auth_err}")
                    return False # Authentication failure
                except smtplib.SMTPException as smtp_err:
                    logging.error(f"SMTP login error: {smtp_err}")
                    return False # Other login error

            server.send_message(msg)
        return True # Indicate success
    except smtplib.SMTPConnectError as e:
        logging.error(f"Failed to connect to SMTP server {smtp_config['smtp_server']}:{smtp_config['smtp_port']}. Error: {e}")
        return False
    except smtplib.SMTPServerDisconnected as e:
        logging.error(f"SMTP server disconnected unexpectedly. Error: {e}")
        return False
    except smtplib.SMTPResponseException as e:
        logging.error(f"SMTP server responded with an error: {e.smtp_code} - {e.smtp_error}")
        return False
    except TimeoutError: # Catch potential timeout during connection or sending
        logging.error(f"Timeout occurred while sending email notification to {smtp_config['to_address']}.")
        return False
    except Exception as e:
        # Catch any other unexpected exceptions during email sending
        logging.error(f"Failed to send email notification: {str(e)}", exc_info=True) # Add exc_info for detailed traceback
        return False # Indicate failure

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
            pass
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

def get_overseerr_scan_job_id(overseerr_url, overseerr_api_key, overseerr_instance_id):
    """
    Retrieves the Job ID for a Plex scan task from Overseerr.
    Caches the Job ID to avoid repeated API calls.
    """
    if overseerr_instance_id in overseerr_job_id_cache:
        cached_job_id = overseerr_job_id_cache[overseerr_instance_id]
        return cached_job_id

    api_endpoint = f"{overseerr_url}/api/v1/settings/jobs"
    headers = {"X-Api-Key": overseerr_api_key, "Accept": "application/json"}
    
    try:
        response = requests.get(api_endpoint, headers=headers, timeout=15)
        response.raise_for_status()
        jobs = response.json()
        
        # Prioritize "plex-recently-added-scan"
        preferred_job_id = "plex-recently-added-scan"
        
        for job in jobs:
            if job.get("id") == preferred_job_id:
                overseerr_job_id_cache[overseerr_instance_id] = preferred_job_id
                return preferred_job_id

        # If preferred ID is not found, fall back to keyword search
        logging.warning(f"Overseerr: Preferred job ID '{preferred_job_id}' not found. Falling back to keyword search for instance '{overseerr_instance_id}'.")
        
        # Prioritized keywords for Plex-specific scans
        plex_keywords = ["plex sync", "plex scan", "recently added"] # Added "recently added"
        # Fallback keywords for general library scans
        general_scan_keywords = ["full scan", "library scan", "media scan", "scan disk", "scan files"]

        found_job_id = None

        # First pass: Plex-specific keywords
        for job in jobs:
            job_name_lower = job.get("name", "").lower()
            # Check ID as well for plex_keywords
            job_id_lower = job.get("id", "").lower()
            if any(keyword in job_name_lower for keyword in plex_keywords) or \
               any(keyword.replace(" ", "-") in job_id_lower for keyword in plex_keywords):
                found_job_id = job.get("id")
                break
        
        # Second pass: General scan keywords if no Plex-specific one was found
        if not found_job_id:
            for job in jobs:
                job_name_lower = job.get("name", "").lower()
                # Check ID as well for general_scan_keywords
                job_id_lower = job.get("id", "").lower()
                if any(keyword in job_name_lower for keyword in general_scan_keywords) or \
                   any(keyword.replace(" ", "-") in job_id_lower for keyword in general_scan_keywords):
                    found_job_id = job.get("id")
                    break

        if found_job_id:
            overseerr_job_id_cache[overseerr_instance_id] = found_job_id
            return found_job_id
        else:
            logging.warning(f"Overseerr: Could not find a suitable Plex or general scan job for instance '{overseerr_instance_id}' at {overseerr_url} even after keyword search.")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Overseerr: Error getting job list from instance '{overseerr_instance_id}': {e}")
        return None
    except ValueError as e: # Handles JSON decoding errors
        logging.error(f"Overseerr: Error decoding JSON response for job list from instance '{overseerr_instance_id}': {e}")
        return None

def trigger_overseerr_scan(overseerr_url, overseerr_api_key, overseerr_instance_id, overseerr_display_name):
    """
    Triggers a library scan job in the specified Overseerr instance.
    """
    global overseerr_scan_schedulers # Ensure we can modify this global
    

    job_id = get_overseerr_scan_job_id(overseerr_url, overseerr_api_key, overseerr_instance_id)

    if not job_id:
        logging.warning(f"Overseerr: Cannot trigger scan for instance '{overseerr_display_name}', no suitable job ID found.")
        if overseerr_instance_id in overseerr_scan_schedulers: # Clean up timer if job ID couldn't be found
            del overseerr_scan_schedulers[overseerr_instance_id]
        return

    api_endpoint = f"{overseerr_url}/api/v1/settings/jobs/{job_id}/run"
    headers = {"X-Api-Key": overseerr_api_key, "Accept": "application/json"}

    try:
        logging.info(f"Overseerr: Triggering job ID '{job_id}' (Plex/Library Scan) for instance '{overseerr_display_name}' at {api_endpoint}")
        response = requests.post(api_endpoint, headers=headers, timeout=10)
        response.raise_for_status()
        logging.info(f"Overseerr: Successfully triggered scan (Job ID: {job_id}) for instance '{overseerr_display_name}'. Response: {response.status_code}")
    except requests.exceptions.HTTPError as e:
        logging.error(f"Overseerr: HTTP error triggering scan (Job ID: {job_id}) for instance '{overseerr_display_name}': {e}. Response: {e.response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Overseerr: Request exception triggering scan (Job ID: {job_id}) for instance '{overseerr_display_name}': {e}")
    finally:
        # Remove the timer from the dict after attempting to trigger the scan
        if overseerr_instance_id in overseerr_scan_schedulers:
            try:
                # If the timer object is still there and is the one we are finishing
                # it's good practice to ensure it's cancelled, though it should have finished.
                # For simplicity, just deleting the key is often enough if the timer function only runs once.
                del overseerr_scan_schedulers[overseerr_instance_id]
            except KeyError:
                logging.warning(f"Overseerr: Scan scheduler for instance '{overseerr_instance_id}' already removed.")


def handle_overseerr_scan_scheduling():
    """
    Checks for configured Overseerr instances and schedules a delayed Plex scan if one isn't already pending.
    """
    global overseerr_scan_schedulers # Ensure we can modify this global
    
    overseerr_instances = get_overseerr_instances()
    if not overseerr_instances:
        logging.debug("Overseerr: No enabled Overseerr instances found for scan scheduling.")
        return

    logging.debug(f"Overseerr: Found {len(overseerr_instances)} instance(s) for potential scan scheduling.")

    for instance in overseerr_instances:
        instance_id = instance['id']
        instance_url = instance['url']
        instance_api_key = instance['api_key']
        instance_display_name = instance.get('display_name', instance_id)

        if instance_id in overseerr_scan_schedulers:
            # Check if the existing timer is still alive; if not, we can schedule a new one.
            # This handles cases where a timer might have been created but the trigger function failed before removing it.
            timer = overseerr_scan_schedulers[instance_id]
            if timer.is_alive():
                continue
        
        new_timer = Timer(
            OVERSEERR_SCAN_DELAY,
            trigger_overseerr_scan,
            args=[instance_url, instance_api_key, instance_id, instance_display_name]
        )
        new_timer.daemon = True # Ensure timer doesn't block program exit
        overseerr_scan_schedulers[instance_id] = new_timer
        new_timer.start()