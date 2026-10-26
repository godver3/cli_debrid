"""A lone old episode is scraped as a single episode first, not forced into pack mode.

Episodes older than 7 days were always scraped in multi-pack mode first, which rejects
every single-episode release. For one episode moved back to Wanted (Squid Game S03E05,
the rest of the season Collected) that overrode the eligibility check's "no other
episodes pending - single episode scrape" and picked a season pack. The force now
only applies when another episode of the same season is Wanted or Scraping; a lone
episode falls back to packs only if the single scrape finds nothing.
"""
from unittest import mock

import database
from queues.scraping_queue import _other_season_episodes_pending

ITEM = {'id': 5, 'imdb_id': 'tt1', 'type': 'episode', 'season_number': 3, 'episode_number': 5, 'state': 'Scraping'}


def _ep(id_, season, state):
    return {'id': id_, 'imdb_id': 'tt1', 'type': 'episode', 'season_number': season,
            'episode_number': id_, 'state': state}


def check(rows):
    with mock.patch.object(database, 'get_all_media_items', return_value=rows):
        return _other_season_episodes_pending(ITEM)


def test_lone_episode_with_rest_of_season_collected():
    assert not check([dict(ITEM)] + [_ep(i, 3, 'Collected') for i in (1, 2, 3, 4, 6)])


def test_other_episode_of_the_season_wanted():
    assert check([dict(ITEM), _ep(6, 3, 'Wanted')])
    assert check([dict(ITEM), _ep(6, 3, 'Scraping')])


def test_pending_episode_in_another_season_does_not_count():
    assert not check([dict(ITEM), _ep(9, 2, 'Wanted')])


def test_lookup_error_keeps_pack_first():
    with mock.patch.object(database, 'get_all_media_items', side_effect=RuntimeError('db locked')):
        assert _other_season_episodes_pending(ITEM)
