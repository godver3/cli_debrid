import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from utilities import recommendation_provider


@contextmanager
def provider_modules(trakt_config, scrob_config):
    trakt = types.ModuleType('content_checkers.trakt')
    trakt.get_trakt_config = lambda: trakt_config
    scrob = types.ModuleType('content_checkers.scrob')
    scrob.get_scrob_config = lambda: scrob_config
    with patch.dict(sys.modules, {
        'content_checkers.trakt': trakt,
        'content_checkers.scrob': scrob,
    }):
        yield


class TestRecommendationProviderSelection(unittest.TestCase):
    @patch('utilities.recommendation_provider.get_setting')
    def test_trakt_is_preferred_when_both_are_configured(self, get_setting):
        get_setting.side_effect = lambda section, key, default='': {
            ('Trakt', 'client_id'): 'client-id',
            ('Trakt', 'client_secret'): 'client-secret',
        }.get((section, key), default)

        with provider_modules(
            {'OAUTH_TOKEN': 'token'},
            {'base_url': 'http://scrob', 'api_key': 'key'},
        ):
            self.assertEqual(recommendation_provider.get_recommendation_provider(), 'trakt')

    @patch('utilities.recommendation_provider.get_setting', return_value='')
    def test_scrob_replaces_disabled_trakt_even_with_stale_legacy_token(self, _setting):
        with provider_modules(
            {'OAUTH_TOKEN': 'stale-token'},
            {'base_url': 'http://scrob', 'api_key': 'key'},
        ):
            self.assertEqual(recommendation_provider.get_recommendation_provider(), 'scrob')

    @patch('utilities.recommendation_provider.get_setting', return_value='')
    def test_no_provider_when_neither_is_configured(self, _setting):
        with provider_modules({}, None):
            self.assertIsNone(recommendation_provider.get_recommendation_provider())


if __name__ == '__main__':
    unittest.main()
