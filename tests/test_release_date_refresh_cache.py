import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from cli_battery.app import direct_api
from cli_battery.app.database import Item, Metadata


class TestMovieReleaseDateCacheAge(unittest.TestCase):
    def _session_for(self, item, metadata):
        session = MagicMock()

        item_query = MagicMock()
        item_query.options.return_value.filter_by.return_value.first.return_value = item

        metadata_query = MagicMock()
        metadata_query.filter_by.return_value.first.return_value = metadata

        def query(model):
            if model is Item:
                return item_query
            if model is Metadata:
                return metadata_query
            raise AssertionError(f"Unexpected model queried: {model}")

        session.query.side_effect = query
        session_context = MagicMock()
        session_context.__enter__.return_value = session
        session_context.__exit__.return_value = False
        return session, session_context

    def _cached_item(self, cache_age):
        now = datetime.now(timezone.utc)
        metadata = MagicMock()
        metadata.key = 'release_dates'
        metadata.value = json.dumps({'US': [{'date': '2026-01-01', 'type': 'theatrical'}]})
        metadata.last_updated = now - cache_age

        item = MagicMock()
        item.id = 42
        item.media_status = 'released'
        item.last_trakt_fetch = now
        item.item_metadata = [metadata]
        return item, metadata

    def test_daily_policy_uses_release_date_cache_when_under_one_day_old(self):
        item, metadata = self._cached_item(timedelta(hours=23))
        _, session_context = self._session_for(item, metadata)
        provider = MagicMock()

        with patch.object(direct_api, 'managed_session', return_value=session_context), \
             patch.object(direct_api, '_get_metadata_client', return_value=provider):
            releases, source = direct_api.DirectAPI.get_movie_release_dates(
                'tt1234567',
                max_cache_age=timedelta(days=1),
            )

        self.assertEqual('battery', source)
        self.assertEqual('2026-01-01', releases['US'][0]['date'])
        provider.get_movie_release_dates.assert_not_called()

    def test_daily_policy_refreshes_release_dates_after_one_day(self):
        item, metadata = self._cached_item(timedelta(hours=25))
        _, session_context = self._session_for(item, metadata)
        provider = MagicMock()
        provider.get_movie_release_dates.return_value = {
            'US': [{'date': '2026-08-20', 'type': 'digital'}],
        }

        with patch.object(direct_api, 'managed_session', return_value=session_context), \
             patch.object(direct_api, '_get_metadata_client', return_value=provider), \
             patch.object(direct_api, '_get_metadata_source_name', return_value='tvdb'):
            releases, source = direct_api.DirectAPI.get_movie_release_dates(
                'tt1234567',
                max_cache_age=timedelta(days=1),
            )

        self.assertEqual('tvdb', source)
        self.assertEqual('digital', releases['US'][0]['type'])
        provider.get_movie_release_dates.assert_called_once_with('tt1234567')
        self.assertEqual(releases, json.loads(metadata.value))

    def test_default_movie_policy_remains_unchanged(self):
        item, metadata = self._cached_item(timedelta(days=30))
        _, session_context = self._session_for(item, metadata)
        provider = MagicMock()

        with patch.object(direct_api, 'managed_session', return_value=session_context), \
             patch.object(direct_api, '_get_metadata_client', return_value=provider):
            releases, source = direct_api.DirectAPI.get_movie_release_dates('tt1234567')

        self.assertEqual('battery', source)
        self.assertEqual('theatrical', releases['US'][0]['type'])
        provider.get_movie_release_dates.assert_not_called()

    def test_daily_policy_preserves_stale_dates_when_provider_fails(self):
        item, metadata = self._cached_item(timedelta(days=2))
        old_checked_at = metadata.last_updated
        _, session_context = self._session_for(item, metadata)
        provider = MagicMock()
        provider.get_movie_release_dates.return_value = None

        with patch.object(direct_api, 'managed_session', return_value=session_context), \
             patch.object(direct_api, '_get_metadata_client', return_value=provider):
            releases, source = direct_api.DirectAPI.get_movie_release_dates(
                'tt1234567',
                max_cache_age=timedelta(days=1),
            )

        self.assertEqual('battery', source)
        self.assertEqual('theatrical', releases['US'][0]['type'])
        self.assertEqual(old_checked_at, metadata.last_updated)
        provider.get_movie_release_dates.assert_called_once_with('tt1234567')


if __name__ == '__main__':
    unittest.main()
