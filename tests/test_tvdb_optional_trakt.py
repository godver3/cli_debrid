import unittest
from unittest.mock import patch

from cli_battery.app import tvdb_client


class TestTVDBOptionalTraktStatus(unittest.TestCase):
    def test_blank_client_id_skips_trakt_request(self):
        with patch('utilities.settings.get_setting', return_value=''), patch(
            'cli_battery.app.trakt_client._make_request'
        ) as trakt_request:
            result = tvdb_client._get_trakt_status('tt10293938')

        self.assertIsNone(result)
        trakt_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
