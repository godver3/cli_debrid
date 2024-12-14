import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from settings_schema import SETTINGS_SCHEMA
from collections import defaultdict
from datetime import datetime

def consolidate_items(notifications):
    consolidated = defaultdict(lambda: defaultdict(list))
    
    # First pass: organize all items
    for notification in notifications:
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
            item_key = f"{key}_{item_info['episode']}"
        elif media_type == 'season':
            season_number = notification.get('season_number', '')
            item_info['season'] = f"Season {season_number}"
            item_key = f"{key}_{item_info['season']}"
        else:
            item_key = key
            
        # Store in consolidated with the unique key
        if media_type in ['movie']:
            consolidated['movie'][item_key] = [item_info]
        else:
            consolidated['show'][item_key] = [item_info]
            
    return consolidated

def format_notification_content(notifications, notification_type):
    consolidated = consolidate_items(notifications)
    
    content = []
    
    if notification_type == 'Discord':
        movie_emoji = "🎬"
        tv_emoji = "📺"
        tv_show_emoji = "⭐"
        movies_emoji = "⭐"
        upgrade_emoji = "⬆️"
        new_emoji = "🆕"

        if consolidated['movie']:
            content.append(f"\n{movie_emoji} **Movies collected/upgraded:**\n")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items))
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} **{movie}** [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} **{movie}** [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append(f"\n{tv_emoji} **TV Shows collected/upgraded:**\n")
            for show, items in consolidated['show'].items():
                content.append(f"{tv_show_emoji} **{show}**")
                seasons = [item['season'] for item in items if 'season' in item]
                episodes = [item['episode'] for item in items if 'episode' in item]
                versions = set(item['version'] for item in items)
                
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                    
                    if 'season' in item:
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})")
                    elif 'episode' in item:
                        content.append(f"    {status_emoji} {item['episode']} [{item['version']}] ({status})")
                    else:
                        content.append(f"    {status_emoji} Full show [{item['version']}] ({status})")
                
                content.append(f"    - Versions: [{', '.join(versions)}]\n")
    elif notification_type == 'Email':
        movie_emoji = "🎬"
        tv_emoji = "📺"
        tv_show_emoji = "⭐"
        movies_emoji = "⭐"
        upgrade_emoji = "⬆️"
        new_emoji = "🆕"

        if consolidated['movie']:
            content.append(f"\n{movie_emoji} <b>Movies collected/upgraded:</b><br><br>")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items))
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} <b>{movie}</b> [{versions}] (Upgraded, originally collected on {collected_date})<br>")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} <b>{movie}</b> [{versions}] (New collection)<br>")
        
        if consolidated['show']:
            content.append(f"\n<br>{tv_emoji} <b>TV Shows collected/upgraded:</b><br><br>")
            for show, items in consolidated['show'].items():
                content.append(f"{tv_show_emoji} <b>{show}</b><br>")
                seasons = [item['season'] for item in items if 'season' in item]
                episodes = [item['episode'] for item in items if 'episode' in item]
                versions = set(item['version'] for item in items)
                
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                    
                    if 'season' in item:
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})<br>")
                    elif 'episode' in item:
                        content.append(f"    {status_emoji} {item['episode']} [{item['version']}] ({status})<br>")
                    else:
                        content.append(f"    {status_emoji} Full show [{item['version']}] ({status})<br>")
                
                content.append(f"    - Versions: [{', '.join(versions)}]<br><br>")
    elif notification_type == 'Telegram':
        movie_emoji = "🎬"
        tv_emoji = "📺"
        tv_show_emoji = "⭐"
        movies_emoji = "⭐"
        upgrade_emoji = "⬆️"
        new_emoji = "🆕"

        if consolidated['movie']:
            content.append(f"\n{movie_emoji} <b>Movies collected/upgraded:</b>\n")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items))
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} <i>{movie}</i> [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} <i>{movie}</i> [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append(f"\n{tv_emoji} <b>TV Shows collected/upgraded:</b>\n")
            for show, items in consolidated['show'].items():
                content.append(f"{tv_show_emoji} <i>{show}</i>")
                seasons = [item['season'] for item in items if 'season' in item]
                episodes = [item['episode'] for item in items if 'episode' in item]
                versions = set(item['version'] for item in items)
                
                for item in items:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                    
                    if 'season' in item:
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})")
                    elif 'episode' in item:
                        content.append(f"    {status_emoji} {item['episode']} [{item['version']}] ({status})")
                    else:
                        content.append(f"    {status_emoji} Full show [{item['version']}] ({status})")
                
                content.append(f"    - Versions: [{', '.join(versions)}]\n")
    else:
        if consolidated['movie']:
            content.append("Movies collected/upgraded:\n")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items))
                for item in items:
                    status = "Upgraded" if item['is_upgrade'] else "New collection"
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"  • {movie} [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"  • {movie} [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append("\nTV Shows collected/upgraded:")
            for show, items in consolidated['show'].items():
                content.append(f"• {show}")
                seasons = [item['season'] for item in items if 'season' in item]
                episodes = [item['episode'] for item in items if 'episode' in item]
                versions = set(item['version'] for item in items)
                
                for item in items:
                    status = "Upgraded" if item['is_upgrade'] else "New collection"
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    
                    if 'season' in item:
                        content.append(f"    - {item['season']} [{item['version']}] ({status}, originally collected on {collected_date})")
                    elif 'episode' in item:
                        content.append(f"    - {item['episode']} [{item['version']}] ({status}, originally collected on {collected_date})")
                    else:
                        content.append(f"    - Full show [{item['version']}] ({status}, originally collected on {collected_date})")
                
                content.append(f"    - Versions: [{', '.join(versions)}]\n")

    return "\n".join(content)

def send_notifications(notifications, enabled_notifications):
    for notification_id, notification_config in enabled_notifications.items():
        if not notification_config.get('enabled', False):
            continue

        notification_type = notification_config['type']
        content = format_notification_content(notifications, notification_type)
        
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
            
            else:
                logging.warning(f"Unknown notification type: {notification_type}")
        except Exception as e:
            logging.error(f"Error sending {notification_type} notification: {str(e)}")

    logging.info(f"Notification process completed")

def send_discord_notification(webhook_url, content):
    MAX_LENGTH = 1900  # Leave some room for formatting

    def send_chunk(chunk):
        payload = {"content": chunk}
        response = requests.post(webhook_url, json=payload)
        response.raise_for_status()
        logging.info(f"Discord notification chunk sent successfully")

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
                chunk_content = f"Message part {i}/{len(chunks)}:\n\n{chunk}"
                send_chunk(chunk_content)

        logging.info(f"Discord notification sent successfully")
    except Exception as e:
        logging.error(f"Failed to send Discord notification: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response content: {e.response.content}")

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