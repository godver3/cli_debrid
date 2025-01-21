import unittest
from notifications import format_notification_content

class TestNotifications(unittest.TestCase):
    def test_state_change_notification_with_upgrading(self):
        # Test data for a movie being upgraded
        notifications = [{
            'id': 1,
            'title': 'Test Movie',
            'type': 'movie',
            'year': '2023',
            'version': '2160p HDR',
            'new_state': 'Checking',
            'upgrading': True,
            'upgrading_from': '1080p'
        }]

        # Test Discord format (with movie emoji)
        discord_content = format_notification_content(notifications, 'Discord', 'state_change')
        self.assertIn('🎬 Test Movie (2023) [2160p HDR] → Upgrading', discord_content)

        # Test Email format (HTML with bold tags and movie emoji)
        email_content = format_notification_content(notifications, 'Email', 'state_change')
        self.assertIn('🎬 <b>Test Movie (2023)</b> [2160p HDR] → Upgrading', email_content)

        # Test Telegram format (HTML with italics and movie emoji)
        telegram_content = format_notification_content(notifications, 'Telegram', 'state_change')
        self.assertIn('🎬 <i>Test Movie (2023)</i> [2160p HDR] → Upgrading', telegram_content)

        # Test NTFY format (plain text with movie emoji)
        ntfy_content = format_notification_content(notifications, 'NTFY', 'state_change')
        self.assertIn('🎬 Test Movie (2023) [2160p HDR] → Upgrading', ntfy_content)

        # Test plain text format (with bullet point)
        plain_content = format_notification_content(notifications, 'PlainText', 'state_change')
        self.assertIn('• Test Movie (2023) [2160p HDR] → Upgrading', plain_content)

    def test_state_change_notification_without_upgrading(self):
        # Test data for a movie changing state without upgrading
        notifications = [{
            'id': 1,
            'title': 'Test Movie',
            'type': 'movie',
            'year': '2023',
            'version': '2160p HDR',
            'new_state': 'Checking',
            'upgrading': False
        }]

        # Test Discord format (with movie emoji)
        discord_content = format_notification_content(notifications, 'Discord', 'state_change')
        self.assertIn('🎬 Test Movie (2023) [2160p HDR] → Checking', discord_content)

    def test_state_change_notification_with_tv_show_upgrading(self):
        # Test data for a TV episode being upgraded
        notifications = [{
            'id': 1,
            'title': 'Test Show',
            'type': 'episode',
            'season_number': 1,
            'episode_number': 1,
            'version': '2160p HDR',
            'new_state': 'Checking',
            'upgrading': True,
            'upgrading_from': '1080p'
        }]

        # Test Discord format (with TV emoji)
        discord_content = format_notification_content(notifications, 'Discord', 'state_change')
        self.assertIn('📺 Test Show S01E01 [2160p HDR] → Upgrading', discord_content)

if __name__ == '__main__':
    unittest.main() 