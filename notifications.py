import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from settings_schema import SETTINGS_SCHEMA
from collections import defaultdict
from datetime import datetime
import time

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
        'new': "🆕"
    }

    def format_state_suffix(state, is_upgrade=False):
        """Return the appropriate suffix based on state"""
        if state in ['Collected', 'Upgraded']:
            return f"({'Upgraded' if is_upgrade else 'Collected'})"
        else:
            return f"→ {state}"

    def format_title(item):
        """Format the title with appropriate prefix and formatting."""
        title = item.get('title', '')
        year = item.get('year', '')
        version = item.get('version', '').strip('*')
        is_upgrade = item.get('is_upgrade', False)
        media_type = item.get('media_type', 'movie')
        new_state = item.get('new_state', '')
        
        # For both movies and TV shows, use NEW, arrow, or default icon
        if is_upgrade and new_state == 'Collected':
            prefix = '⬆️'
        elif new_state == 'Collected':
            prefix = '🆕'
        else:
            prefix = '🎬'
        
        # Add version info for movies
        if media_type == 'movie':
            return f"{prefix} **{title}** ({year}) [{version}]"
        else:
            return f"{prefix} **{title}** ({year})"

    def format_episode(item):
        """Format episode information"""
        season = item.get('season_number')
        episode = item.get('episode_number')
        if season is not None and episode is not None:
            version = item.get('version', '')
            version_str = f" [{version}]" if version else ""
            return f"    S{season:02d}E{episode:02d}{version_str}"
        return None

    # Group items by show/movie
    grouped_items = {}
    for item in notifications:
        key = (item.get('title'), item.get('type'), item.get('year'))
        if key not in grouped_items:
            grouped_items[key] = []
        grouped_items[key].append(item)

    content = []
    
    # Process each group
    for (title, type_, year), items in sorted(grouped_items.items()):
        if type_ == 'movie':
            # For movies, each item is a separate line
            for item in items:
                state = item.get('new_state', 'Collected')
                is_upgrade = item.get('is_upgrade', False)
                line = f"{format_title(item)} {format_state_suffix(state, is_upgrade)}"
                content.append(line)
        else:
            # For shows, group episodes under the show title
            show_item = items[0]  # Use first item for show details
            content.append(format_title(show_item))
            
            # Add episodes
            episode_lines = []
            for item in sorted(items, key=lambda x: (x.get('season_number', 0), x.get('episode_number', 0))):
                episode_line = format_episode(item)
                if episode_line:
                    state = item.get('new_state', 'Collected')
                    is_upgrade = item.get('is_upgrade', False)
                    episode_lines.append(f"{episode_line} {format_state_suffix(state, is_upgrade)}")
            
            content.extend(episode_lines)

        # Add spacing between different shows/movies
        if (title, type_, year) != list(grouped_items.keys())[-1]:
            content.append("")

    return "\n".join(content)

def send_notifications(notifications, enabled_notifications, notification_category='collected'):
    for notification_id, notification_config in enabled_notifications.items():
        if not notification_config.get('enabled', False):
            continue

        # Check if this notification category is enabled for this notification type
        notify_on = notification_config.get('notify_on', {})
        if not notify_on.get(notification_category, True):  # Default to True for backward compatibility
            logging.debug(f"Skipping {notification_id} notification: {notification_category} notifications are disabled")
            continue

        notification_type = notification_config['type']
        content = format_notification_content(notifications, notification_type, notification_category)
        
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
        except Exception as e:
            logging.error(f"Error sending {notification_type} notification: {str(e)}")

    logging.info(f"Notification process completed")

def send_discord_notification(webhook_url, content):
    MAX_LENGTH = 1900  # Leave some room for formatting
    MAX_RETRIES = 3
    BASE_DELAY = 0.5  # Base delay in seconds

    # Check for empty content
    if not content or not content.strip():
        logging.warning("Skipping Discord notification: content is empty")
        return

    def send_chunk(chunk, attempt=1):
        # Check for empty chunk
        if not chunk or not chunk.strip():
            logging.warning("Skipping empty chunk in Discord notification")
            return

        try:
            payload = {"content": chunk}
            response = requests.post(webhook_url, json=payload)
            
            if response.status_code == 429:  # Rate limit hit
                retry_after = float(response.json().get('retry_after', BASE_DELAY))
                if attempt < MAX_RETRIES:
                    logging.warning(f"Discord rate limit hit, waiting {retry_after} seconds before retry {attempt}/{MAX_RETRIES}")
                    time.sleep(retry_after)
                    return send_chunk(chunk, attempt + 1)
                else:
                    raise Exception("Max retries reached for rate limit")
            
            response.raise_for_status()
            logging.info(f"Discord notification chunk sent successfully")
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY * (2 ** (attempt - 1))  # Exponential backoff
                logging.warning(f"Discord request failed, retrying in {delay} seconds (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(delay)
                return send_chunk(chunk, attempt + 1)
            raise

    try:
        if len(content) <= MAX_LENGTH:
            send_chunk(content)
        else:
            chunks = []
            current_chunk = ""
            for line in content.split('\n'):
                if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""
                current_chunk += line + '\n'
            if current_chunk:
                chunks.append(current_chunk)

            for i, chunk in enumerate(chunks, 1):
                if chunk.strip():  # Only send non-empty chunks
                    chunk_content = f"Message part {i}/{len(chunks)}:\n\n{chunk}"
                    send_chunk(chunk_content)
                    # Add a small delay between chunks to help avoid rate limits
                    if i < len(chunks):
                        time.sleep(BASE_DELAY)

        logging.info(f"Discord notification sent successfully")
    except Exception as e:
        logging.error(f"Failed to send Discord notification: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response content: {e.response.content}")
            if e.response.status_code == 400 and 'empty message' in str(e.response.content).lower():
                logging.warning("Attempted to send empty message to Discord - skipping")

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
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": content,
            "parse_mode": "html"
        }
        response = requests.post(url, json=payload)
        response.raise_for_status()
        logging.info(f"Telegram notification sent successfully")
    except Exception as e:
        logging.error(f"Failed to send Telegram notification: {str(e)}")

def verify_notification_config(notification_type, config):
    schema = SETTINGS_SCHEMA['Notifications']['schema'][notification_type]
    for key, value in schema.items():
        if key not in config or not config[key]:
            if value.get('default') is None:  # Consider it required if there's no default value
                return False, f"Missing required field: {key}"
    return True, None