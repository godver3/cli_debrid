import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from settings_schema import SETTINGS_SCHEMA
from collections import defaultdict
from datetime import datetime

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
            # For movies, we need to access one level deeper to get to the list
            consolidated['movie'][key]['items'].append(item_info)
            
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
                versions = ', '.join(set(item['version'] for item in items['items']))
                for item in items['items']:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} **{movie}** [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} **{movie}** [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append(f"\n{tv_emoji} **TV Shows collected/upgraded:**\n")
            for show, seasons in consolidated['show'].items():
                content.append(f"{tv_show_emoji} **{show}**")
                
                # Handle full seasons
                if seasons.get('seasons'):
                    for item in seasons['seasons']:
                        status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                        collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                        status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})")
                
                # Handle individual episodes
                for season, episodes in seasons.items():
                    if season == 'seasons':
                        continue
                        
                    if len(episodes) > 0:
                        # Group episodes with same version and upgrade status
                        episode_groups = defaultdict(list)
                        for ep in episodes:
                            key = (ep['version'], ep['is_upgrade'], ep['original_collected_at'])
                            episode_groups[key].append(ep['episode'])
                        
                        for (version, is_upgrade, collected_at), eps in episode_groups.items():
                            status_emoji = upgrade_emoji if is_upgrade else new_emoji
                            collected_date = datetime.fromisoformat(collected_at).strftime('%Y-%m-%d')
                            status = f"Upgraded, originally collected on {collected_date}" if is_upgrade else "New collection"
                            
                            # Sort episodes
                            eps.sort(key=lambda x: (
                                int(x.split('E')[0][1:]),  # Season number
                                int(x.split('E')[1])       # Episode number
                            ))
                            episodes_str = ', '.join(eps)
                            content.append(f"    {status_emoji} {episodes_str} [{version}] ({status})")
                
                # Add versions summary
                all_versions = set()
                for season_data in seasons.values():
                    if isinstance(season_data, list):  # Episodes or seasons
                        all_versions.update(item['version'] for item in season_data)
                
                if all_versions:
                    content.append(f"    Versions: [{', '.join(sorted(all_versions))}]\n")
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
                versions = ', '.join(set(item['version'] for item in items['items']))
                for item in items['items']:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} <b>{movie}</b> [{versions}] (Upgraded, originally collected on {collected_date})<br>")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} <b>{movie}</b> [{versions}] (New collection)<br>")
        
        if consolidated['show']:
            content.append(f"\n<br>{tv_emoji} <b>TV Shows collected/upgraded:</b><br><br>")
            for show, seasons in consolidated['show'].items():
                content.append(f"{tv_show_emoji} <b>{show}</b><br>")
                
                # Handle full seasons
                if seasons.get('seasons'):
                    for item in seasons['seasons']:
                        status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                        collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                        status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})<br>")
                
                # Handle individual episodes
                for season, episodes in seasons.items():
                    if season == 'seasons':
                        continue
                        
                    if len(episodes) > 0:
                        # Group episodes with same version and upgrade status
                        episode_groups = defaultdict(list)
                        for ep in episodes:
                            key = (ep['version'], ep['is_upgrade'], ep['original_collected_at'])
                            episode_groups[key].append(ep['episode'])
                        
                        for (version, is_upgrade, collected_at), eps in episode_groups.items():
                            status_emoji = upgrade_emoji if is_upgrade else new_emoji
                            collected_date = datetime.fromisoformat(collected_at).strftime('%Y-%m-%d')
                            status = f"Upgraded, originally collected on {collected_date}" if is_upgrade else "New collection"
                            
                            # Sort episodes
                            eps.sort(key=lambda x: (
                                int(x.split('E')[0][1:]),  # Season number
                                int(x.split('E')[1])       # Episode number
                            ))
                            episodes_str = ', '.join(eps)
                            content.append(f"    {status_emoji} {episodes_str} [{version}] ({status})<br>")
                
                # Add versions summary
                all_versions = set()
                for season_data in seasons.values():
                    if isinstance(season_data, list):  # Episodes or seasons
                        all_versions.update(item['version'] for item in season_data)
                
                if all_versions:
                    content.append(f"    - Versions: [{', '.join(sorted(all_versions))}]<br><br>")
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
                versions = ', '.join(set(item['version'] for item in items['items']))
                for item in items['items']:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} <i>{movie}</i> [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} <i>{movie}</i> [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append(f"\n{tv_emoji} <b>TV Shows collected/upgraded:</b>\n")
            for show, seasons in consolidated['show'].items():
                content.append(f"{tv_show_emoji} <i>{show}</i>")
                
                # Handle full seasons
                if seasons.get('seasons'):
                    for item in seasons['seasons']:
                        status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                        collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                        status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})")
                
                # Handle individual episodes
                for season, episodes in seasons.items():
                    if season == 'seasons':
                        continue
                        
                    if len(episodes) > 0:
                        # Group episodes with same version and upgrade status
                        episode_groups = defaultdict(list)
                        for ep in episodes:
                            key = (ep['version'], ep['is_upgrade'], ep['original_collected_at'])
                            episode_groups[key].append(ep['episode'])
                        
                        for (version, is_upgrade, collected_at), eps in episode_groups.items():
                            status_emoji = upgrade_emoji if is_upgrade else new_emoji
                            collected_date = datetime.fromisoformat(collected_at).strftime('%Y-%m-%d')
                            status = f"Upgraded, originally collected on {collected_date}" if is_upgrade else "New collection"
                            
                            # Sort episodes
                            eps.sort(key=lambda x: (
                                int(x.split('E')[0][1:]),  # Season number
                                int(x.split('E')[1])       # Episode number
                            ))
                            episodes_str = ', '.join(eps)
                            content.append(f"    {status_emoji} {episodes_str} [{version}] ({status})")
                
                # Add versions summary
                all_versions = set()
                for season_data in seasons.values():
                    if isinstance(season_data, list):  # Episodes or seasons
                        all_versions.update(item['version'] for item in season_data)
                
                if all_versions:
                    content.append(f"    - Versions: [{', '.join(sorted(all_versions))}]\n")
    elif notification_type == 'NTFY':
        movie_emoji = "🎬"
        tv_emoji = "📺"
        tv_show_emoji = "⭐"
        movies_emoji = "⭐"
        upgrade_emoji = "⬆️"
        new_emoji = "🆕"

        if consolidated['movie']:
            content.append(f"\n{movie_emoji} Movies collected/upgraded:\n")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items['items']))
                for item in items['items']:
                    status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"{movies_emoji} {status_emoji} {movie} [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"{movies_emoji} {status_emoji} {movie} [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append(f"\n{tv_emoji} TV Shows collected/upgraded:\n")
            for show, seasons in consolidated['show'].items():
                content.append(f"{tv_show_emoji} {show}")
                
                # Handle full seasons
                if seasons.get('seasons'):
                    for item in seasons['seasons']:
                        status_emoji = upgrade_emoji if item['is_upgrade'] else new_emoji
                        collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                        status = f"Upgraded, originally collected on {collected_date}" if item['is_upgrade'] else "New collection"
                        content.append(f"    {status_emoji} {item['season']} [{item['version']}] ({status})")
                
                # Handle individual episodes
                for season, episodes in seasons.items():
                    if season == 'seasons':
                        continue
                        
                    if len(episodes) > 0:
                        # Group episodes with same version and upgrade status
                        episode_groups = defaultdict(list)
                        for ep in episodes:
                            key = (ep['version'], ep['is_upgrade'], ep['original_collected_at'])
                            episode_groups[key].append(ep['episode'])
                        
                        for (version, is_upgrade, collected_at), eps in episode_groups.items():
                            status_emoji = upgrade_emoji if is_upgrade else new_emoji
                            collected_date = datetime.fromisoformat(collected_at).strftime('%Y-%m-%d')
                            status = f"Upgraded, originally collected on {collected_date}" if is_upgrade else "New collection"
                            
                            # Sort episodes
                            eps.sort(key=lambda x: (
                                int(x.split('E')[0][1:]),  # Season number
                                int(x.split('E')[1])       # Episode number
                            ))
                            episodes_str = ', '.join(eps)
                            content.append(f"    {status_emoji} {episodes_str} [{version}] ({status})")
                
                # Add versions summary
                all_versions = set()
                for season_data in seasons.values():
                    if isinstance(season_data, list):  # Episodes or seasons
                        all_versions.update(item['version'] for item in season_data)
                
                if all_versions:
                    content.append(f"    - Versions: [{', '.join(sorted(all_versions))}]\n")
    else:
        if consolidated['movie']:
            content.append("Movies collected/upgraded:\n")
            for movie, items in consolidated['movie'].items():
                versions = ', '.join(set(item['version'] for item in items['items']))
                for item in items['items']:
                    status = "Upgraded" if item['is_upgrade'] else "New collection"
                    collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                    if item['is_upgrade']:
                        content.append(f"  • {movie} [{versions}] (Upgraded, originally collected on {collected_date})")
                    else:
                        content.append(f"  • {movie} [{versions}] (New collection)")
        
        if consolidated['show']:
            content.append("\nTV Shows collected/upgraded:")
            for show, seasons in consolidated['show'].items():
                content.append(f"• {show}")
                
                # Handle full seasons
                if seasons.get('seasons'):
                    for item in seasons['seasons']:
                        status = "Upgraded" if item['is_upgrade'] else "New collection"
                        collected_date = datetime.fromisoformat(item['original_collected_at']).strftime('%Y-%m-%d')
                        content.append(f"    - {item['season']} [{item['version']}] ({status}, originally collected on {collected_date})")
                
                # Handle individual episodes
                for season, episodes in seasons.items():
                    if season == 'seasons':
                        continue
                        
                    if len(episodes) > 0:
                        # Group episodes with same version and upgrade status
                        episode_groups = defaultdict(list)
                        for ep in episodes:
                            key = (ep['version'], ep['is_upgrade'], ep['original_collected_at'])
                            episode_groups[key].append(ep['episode'])
                        
                        for (version, is_upgrade, collected_at), eps in episode_groups.items():
                            status = "Upgraded" if is_upgrade else "New collection"
                            collected_date = datetime.fromisoformat(collected_at).strftime('%Y-%m-%d')
                            
                            # Sort episodes
                            eps.sort(key=lambda x: (
                                int(x.split('E')[0][1:]),  # Season number
                                int(x.split('E')[1])       # Episode number
                            ))
                            episodes_str = ', '.join(eps)
                            content.append(f"    - {episodes_str} [{version}] ({status}, originally collected on {collected_date})")
                
                # Add versions summary
                all_versions = set()
                for season_data in seasons.values():
                    if isinstance(season_data, list):  # Episodes or seasons
                        all_versions.update(item['version'] for item in season_data)
                
                if all_versions:
                    content.append(f"    - Versions: [{', '.join(sorted(all_versions))}]\n")

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