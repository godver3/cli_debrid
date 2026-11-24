"""Notification add/delete, defaults, enabled lists and test sends."""

from flask import jsonify, request, render_template
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from routes.models import admin_required
import traceback
from datetime import datetime
from routes.notifications import send_telegram_notification, send_discord_notification, send_ntfy_notification, send_email_notification
from ._common import _reload_components_after_settings_change, settings_bp

@settings_bp.route('/notifications/delete', methods=['POST'])
@admin_required
def delete_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' in config and notification_id in config['Notifications']:
            del config['Notifications'][notification_id]
            save_config(config)

            # Reload components so the change takes effect mid-run
            _reload_components_after_settings_change('notification')

            logging.info(f"Notification {notification_id} deleted successfully")
            return jsonify({'success': True})
        else:
            logging.warning(f"Failed to delete notification: {notification_id}")
            return jsonify({'success': False, 'error': 'Notification not found'}), 404
    except Exception as e:
        logging.error(f"Error deleting notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/add', methods=['POST'])
@admin_required
def add_notification():
    try:
        notification_data = request.json
        if not notification_data or 'type' not in notification_data:
            return jsonify({'success': False, 'error': 'Invalid notification data'}), 400

        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        notification_type = notification_data['type']
        existing_count = sum(1 for key in config['Notifications'] if key.startswith(f"{notification_type}_"))
        notification_id = f"{notification_type}_{existing_count + 1}"

        notification_title = notification_type.replace('_', ' ').title()

        config['Notifications'][notification_id] = {
            'type': notification_type,
            'enabled': True,
            'title': notification_title,
            'notify_on': {
                'collected': True,
                'wanted': False,
                'scraping': False,
                'adding': False,
                'checking': False,
                'sleeping': False,
                'unreleased': False,
                'blacklisted': False,
                'pending_uncached': False,
                'upgrading': False,
                'program_stop': True,
                'program_crash': True,
                'program_start': True,
                'program_pause': True,
                'program_resume': True,
                'queue_pause': True,
                'queue_resume': True,
                'queue_start': True,
                'queue_stop': True
            }
        }

        # Add default values based on the notification type
        if notification_type == 'Telegram':
            config['Notifications'][notification_id].update({
                'bot_token': '',
                'chat_id': ''
            })
        elif notification_type == 'Discord':
            config['Notifications'][notification_id].update({
                'webhook_url': ''
            })
        elif notification_type == 'NTFY':
            config['Notifications'][notification_id].update({
                'host': '',
                'topic': '',
                'api_key': '',
                'priority': ''
            })
        elif notification_type == 'Email':
            config['Notifications'][notification_id].update({
                'smtp_server': '',
                'smtp_port': 587,
                'smtp_username': '',
                'smtp_password': '',
                'from_address': '',
                'to_address': ''
            })

        save_config(config)

        # Reload components so the change takes effect mid-run
        _reload_components_after_settings_change('notification')

        logging.info(f"Notification {notification_id} added successfully")
        return jsonify({'success': True, 'notification_id': notification_id})
    except Exception as e:
        logging.error(f"Error adding notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def ensure_notification_defaults(notification_config):
    """Ensure notification config has all required default fields."""
    default_categories = {
        'collected': True,
        'wanted': False,
        'scraping': False,
        'adding': False,
        'checking': False,
        'downloading': False,
        'sleeping': False,
        'unreleased': False,
        'blacklisted': False,
        'pending_uncached': False,
        'upgrading': False,
        'program_stop': True,
        'program_crash': True,
        'program_start': True,
        'program_pause': True,
        'program_resume': True,
        'queue_pause': True,
        'queue_resume': True,
        'queue_start': True,
        'queue_stop': True
    }

    # If notify_on is missing or empty, set it to the default values
    if 'notify_on' not in notification_config or not notification_config['notify_on']:
        notification_config['notify_on'] = default_categories.copy()
    else:
        # Ensure all categories exist in notify_on
        for category, default_value in default_categories.items():
            if category not in notification_config['notify_on']:
                notification_config['notify_on'][category] = default_value

    return notification_config

@settings_bp.route('/notifications/content', methods=['GET'])
@admin_required
def notifications_content():
    try:
        config = load_config()
        notification_settings = config.get('Notifications', {})

        # Ensure all notifications have the required defaults
        for notification_id, notification_config in notification_settings.items():
            if notification_config is not None:
                notification_settings[notification_id] = ensure_notification_defaults(notification_config)

        # Always save the config to ensure defaults are persisted
        config['Notifications'] = notification_settings
        save_config(config)

        # Sort notifications by type and then by number
        sorted_notifications = sorted(
            notification_settings.items(),
            key=lambda x: (x[1]['type'], int(x[0].split('_')[-1]))
        )

        template_name = 'settings_tabs/notifications.html'

        html_content = render_template(
            template_name,
            notification_settings=dict(sorted_notifications),
            settings=config,
            settings_schema=SETTINGS_SCHEMA
        )
        
        return jsonify({
            'status': 'success',
            'html': html_content
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'An error occurred while generating notifications content: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500

def collect_enabled_notifications():
    """Enabled notifications that have their required fields, with defaults applied."""
    config = load_config()
    notifications = config.get('Notifications', {})
    
    enabled_notifications = {}
    for notification_id, notification_config in notifications.items():
        if not notification_config:
            continue
        # Ensure defaults are present
        notification_config = ensure_notification_defaults(notification_config)
        
        if notification_config.get('enabled', False):
            # Only include notifications that are enabled and have non-empty required fields
            if notification_config['type'] == 'Discord':
                if notification_config.get('webhook_url'):
                    enabled_notifications[notification_id] = notification_config
            elif notification_config['type'] == 'Email':
                # Only check required fields (smtp_username and smtp_password are optional)
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

@settings_bp.route('/notifications/enabled', methods=['GET'])
@admin_required
def get_enabled_notifications():
    try:
        return jsonify({
            'success': True,
            'enabled_notifications': collect_enabled_notifications()
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/enabled_for_category/<category>', methods=['GET'])
def get_enabled_notifications_for_category(category):
    try:
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            # Ensure defaults are present
            notification_config = ensure_notification_defaults(notification_config)
            
            if notification_config.get('enabled', False):
                # Check if the notification is enabled for this category
                notify_on = notification_config.get('notify_on', {})
                if not notify_on.get(category, False):
                    continue

                # Only include notifications that are enabled and have non-empty required fields
                if notification_config['type'] == 'Discord':
                    if notification_config.get('webhook_url'):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Email':
                    # Only check required fields (smtp_username and smtp_password are optional)
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

        return jsonify({
            'success': True,
            'enabled_notifications': enabled_notifications
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications for category {category}: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@settings_bp.route('/notifications/update_defaults', methods=['POST'])
@admin_required
def update_notification_defaults():
    try:
        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        # Force update all notifications with proper defaults
        for notification_id, notification_config in config['Notifications'].items():
            if notification_config is not None:
                # Remove empty notify_on if it exists
                if 'notify_on' in notification_config and not notification_config['notify_on']:
                    del notification_config['notify_on']
                
                # Apply defaults
                notification_config = ensure_notification_defaults(notification_config)
                config['Notifications'][notification_id] = notification_config

        save_config(config)
        return jsonify({'success': True, 'message': 'Notification defaults updated successfully'})
    except Exception as e:
        logging.error(f"Error updating notification defaults: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/test', methods=['POST'])
@admin_required
def test_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' not in config or notification_id not in config['Notifications']:
            return jsonify({'success': False, 'error': 'Notification not found'}), 404

        notification_config = config['Notifications'][notification_id]
        
        # Create a test notification
        test_notification = {
            'title': 'Test Notification',
            'message': f'This is a test notification from {notification_config["title"]}',
            'type': 'info',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Get the notification type
        notification_type = notification_config['type']
        
        # Send the test notification based on the type
        success = False
        message = "Test notification sent successfully"
        
        try:
            if notification_type == 'Telegram':
                if not notification_config.get('bot_token') or not notification_config.get('chat_id'):
                    return jsonify({'success': False, 'error': 'Missing Telegram configuration'}), 400
                
                content = f"<b>Test Notification</b>\n\nThis is a test message from Clyde. If you're seeing this, your Telegram notifications are working correctly!"
                send_telegram_notification(
                    notification_config['bot_token'],
                    notification_config['chat_id'],
                    content
                )
                success = True
                
            elif notification_type == 'Discord':
                if not notification_config.get('webhook_url'):
                    return jsonify({'success': False, 'error': 'Missing Discord webhook URL'}), 400
                
                content = "**Test Notification**\n\nThis is a test message from Clyde. If you're seeing this, your Discord notifications are working correctly!"
                send_discord_notification(
                    notification_config['webhook_url'],
                    content
                )
                success = True
                
            elif notification_type == 'NTFY':
                if not notification_config.get('host') or not notification_config.get('topic'):
                    return jsonify({'success': False, 'error': 'Missing NTFY configuration'}), 400
                
                content = "Test Notification\n\nThis is a test message from Clyde. If you're seeing this, your NTFY notifications are working correctly!"
                send_ntfy_notification(
                    notification_config['host'],
                    notification_config.get('api_key', ''),
                    notification_config.get('priority', 'low'),
                    notification_config['topic'],
                    content
                )
                success = True
                
            elif notification_type == 'Email':
                # Only smtp_username and smtp_password are optional
                required_fields = ['smtp_server', 'smtp_port', 'from_address', 'to_address']
                missing_fields = [field for field in required_fields if not notification_config.get(field)]
                
                if missing_fields:
                    return jsonify({'success': False, 'error': f'Missing Email configuration: {", ".join(missing_fields)}'}), 400
                
                content = """
                <html>
                <body>
                <h2>Test Notification</h2>
                <p>This is a test message from Clyde. If you're seeing this, your Email notifications are working correctly!</p>
                </body>
                </html>
                """
                
                smtp_config = {
                    'smtp_server': notification_config['smtp_server'],
                    'smtp_port': notification_config['smtp_port'],
                    'smtp_username': notification_config['smtp_username'],
                    'smtp_password': notification_config['smtp_password'],
                    'from_address': notification_config['from_address'],
                    'to_address': notification_config['to_address']
                }
                
                email_result = send_email_notification(smtp_config, content, 'test')
                if email_result:
                    success = True
                else:
                    # Email sending failed - check if it's likely due to authentication
                    if not notification_config.get('smtp_username') or not notification_config.get('smtp_password'):
                        return jsonify({'success': False, 'error': 'SMTP Authentication failed. This server requires authentication - please provide username and password.'}), 400
                    else:
                        return jsonify({'success': False, 'error': 'Email sending failed. Please check your SMTP configuration and credentials.'}), 400
            
            else:
                return jsonify({'success': False, 'error': f'Unknown notification type: {notification_type}'}), 400
                
            if success:
                logging.info(f"Test notification sent successfully for {notification_id}")
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'error': 'Failed to send test notification'}), 500
                
        except Exception as e:
            logging.error(f"Error sending test notification: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': f'Error sending test notification: {str(e)}'}), 500
            
    except Exception as e:
        logging.error(f"Error testing notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
