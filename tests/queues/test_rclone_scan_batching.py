"""Season packs are one media-server scan and one notification call, not one per episode."""
from unittest.mock import patch

import importlib

import database  # noqa: F401  (app import order, avoids a circular import)

rs = importlib.import_module('routes.debug_routes.rclone_scan')


def _ep(folder, n, typ='episode'):
    return {'type': typ, 'title': 'Show', 'location_on_disk': f'{folder}/Show S01E{n:02d}.mkv'}


def _settings(url='http://plex', token='t'):
    values = {'plex_url_for_symlink': url, 'plex_token_for_symlink': token}
    return lambda section, key, default=None: values.get(key, default)


def test_scan_folder_uses_directory_of_video_file(tmp_path):
    assert rs._scan_folder_for_item({'location_on_disk': str(tmp_path / 'a.mkv')}) == str(tmp_path)
    assert rs._scan_folder_for_item({'full_path': str(tmp_path)}) == str(tmp_path)
    assert rs._scan_folder_for_item({}) == ''


def test_season_pack_scans_once():
    items = [_ep('/lib/Show/Season 1', n) for n in range(1, 11)]
    with patch.object(rs, 'get_setting', _settings()), \
         patch('utilities.silo_functions.is_silo_mode', return_value=False), \
         patch.object(rs, 'plex_update_item') as scan:
        rs._flush_plex_scans(items, 't')
    assert scan.call_count == 1


def test_distinct_folders_and_types_scan_separately():
    items = [_ep('/lib/A/Season 1', 1), _ep('/lib/A/Season 2', 1), _ep('/lib/A/Season 2', 2),
             _ep('/lib/M', 1, typ='movie')]
    with patch.object(rs, 'get_setting', _settings()), \
         patch('utilities.silo_functions.is_silo_mode', return_value=False), \
         patch.object(rs, 'plex_update_item') as scan:
        rs._flush_plex_scans(items, 't')
    assert scan.call_count == 3


def test_no_scan_when_unconfigured():
    with patch.object(rs, 'get_setting', _settings('', '')), \
         patch('utilities.silo_functions.is_silo_mode', return_value=False), \
         patch.object(rs, 'plex_update_item') as scan:
        rs._flush_plex_scans([_ep('/x', 1)], 't')
    scan.assert_not_called()


def test_scan_error_does_not_stop_other_folders():
    items = [_ep('/lib/A', 1), _ep('/lib/B', 1)]
    with patch.object(rs, 'get_setting', _settings()), \
         patch('utilities.silo_functions.is_silo_mode', return_value=False), \
         patch.object(rs, 'plex_update_item', side_effect=[RuntimeError('x'), True]) as scan:
        rs._flush_plex_scans(items, 't')
    assert scan.call_count == 2


def test_notifications_sent_once_with_all_items():
    items = [{'title': 'Show', 'episode_number': n} for n in range(1, 4)]
    with patch.object(rs, 'send_notifications') as send, \
         patch.object(rs, 'get_enabled_notifications', return_value={}):
        rs._flush_collected_notifications(items, 't')
        rs._flush_collected_notifications([], 't')
    assert send.call_count == 1
    assert send.call_args[0][0] == items
    assert send.call_args[1]['notification_category'] == 'collected'
