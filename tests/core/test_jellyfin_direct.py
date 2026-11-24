"""Direct Mount run with Jellyfin/Emby (File Management.plex_mode_media_server = 'jellyfin')."""
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from utilities import direct_mount, jellyfin_direct as jd

SETTINGS = {
    ('File Management', 'file_collection_management'): 'Plex',
    ('File Management', 'plex_mode_media_server'): 'jellyfin',
    ('Debug', 'emby_jellyfin_url'): 'http://jf:8096',
    ('Debug', 'emby_jellyfin_token'): 'key',
    ('Plex', 'mounted_file_location'): '/mnt/clid/__all__',
}
LIBS = [{'id': 'm', 'name': 'Movies', 'type': 'movies', 'paths': ['/media/clid/Movies']},
        {'id': 't', 'name': 'TV', 'type': 'tvshows', 'paths': ['/media/clid/TV Shows']}]


def getter(overrides=None):
    values = {**SETTINGS, **(overrides or {})}
    return lambda section, key=None, default=None: values.get((section, key), default)


@pytest.fixture
def settings(monkeypatch):
    def apply(overrides=None):
        g = getter(overrides)
        for mod in (direct_mount, jd):
            monkeypatch.setattr(mod, 'get_setting', g)
        return g
    apply()
    return apply


# --- which server -------------------------------------------------------------------

@pytest.mark.parametrize('mode,server,expected', [
    ('Plex', 'jellyfin', 'jellyfin'), ('Plex', 'silo', 'silo'), ('Plex', 'plex', 'plex'),
    ('Plex', None, 'plex'), ('Plex', 'bogus', 'plex'), ('Symlinked/Local', 'jellyfin', None),
])
def test_direct_mount_server(mode, server, expected):
    g = getter({('File Management', 'file_collection_management'): mode,
                ('File Management', 'plex_mode_media_server'): server})
    assert direct_mount.direct_mount_server(g) == expected
    assert direct_mount.is_direct_mount_jellyfin(g) == (expected == 'jellyfin')
    assert direct_mount.is_direct_mount_without_plex(g) == (expected in ('silo', 'jellyfin'))


def test_is_jellyfin_direct_needs_url_and_key(settings):
    assert jd.is_jellyfin_direct()
    settings({('Debug', 'emby_jellyfin_token'): ''})
    assert not jd.is_jellyfin_direct()


# --- paths ----------------------------------------------------------------------------

def test_paths_move_between_cli_debrid_and_the_server(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    assert jd.local_base() == '/mnt/clid'
    assert jd.to_server_path('/mnt/clid/TV Shows/Rel/ep.mkv', LIBS) == '/media/clid/TV Shows/Rel/ep.mkv'
    assert jd.from_server_path('/media/clid/TV Shows/Rel/ep.mkv', LIBS) == '/mnt/clid/TV Shows/Rel/ep.mkv'
    # same paths on both sides: unchanged
    same = [{'id': 'x', 'name': 'TV', 'type': 'tvshows', 'paths': ['/mnt/clid/TV Shows']}]
    assert jd.to_server_path('/mnt/clid/TV Shows/Rel/ep.mkv', same) == '/mnt/clid/TV Shows/Rel/ep.mkv'


def test_all_folder_release_is_scanned_through_its_mirrored_library_folder(settings, tmp_path, monkeypatch):
    import database  # noqa: F401  (app import order; plex_functions first is circular)
    base = tmp_path / 'clid'
    (base / '__all__' / 'Rel').mkdir(parents=True)
    (base / 'TV Shows' / 'Rel').mkdir(parents=True)
    libs = [{'id': 't', 'name': 'TV', 'type': 'tvshows', 'paths': ['/media/clid/TV Shows']}]
    settings({('Plex', 'mounted_file_location'): f'{base}/__all__'})
    assert jd.to_server_path(str(base / '__all__' / 'Rel'), libs) == '/media/clid/TV Shows/Rel'


# --- scans ----------------------------------------------------------------------------------

class Resp:
    def __init__(self, status=204, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise jd.requests.HTTPError(str(self.status_code))


def test_scan_posts_the_server_folder(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    calls = []
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append((m, p, k)) or Resp(204))
    assert jd.scan_path('/mnt/clid/TV Shows/Rel/ep.mkv', 'episode')
    (method, path, kw), = calls
    assert (method, path) == ('POST', '/Library/Media/Updated')
    assert kw['json'] == {'Updates': [{'Path': '/media/clid/TV Shows/Rel', 'UpdateType': 'Created'}]}


def test_removal_reports_deleted_and_refused_scan_refreshes_the_library(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    calls = []

    def req(m, p, *a, **k):
        calls.append((m, p, k))
        return Resp(500) if p == '/Library/Media/Updated' else Resp(204)
    monkeypatch.setattr(jd, '_request', req)
    assert jd.remove_file('Show', '/mnt/clid/TV Shows/Rel/ep.mkv', 'Ep')
    assert calls[0][2]['json']['Updates'][0]['UpdateType'] == 'Deleted'
    assert calls[1][:2] == ('POST', '/Items/t/Refresh')


def test_path_outside_every_library_refreshes_the_type_libraries(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    calls = []
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append(p) or Resp(204))
    assert jd.scan_path('/elsewhere/Movie/m.mkv', 'movie')
    assert calls == ['/Items/m/Refresh']


def test_get_libraries_reads_virtual_folders(settings, monkeypatch):
    jd._lib_cache.update(at=0.0, key=None, items=None)
    body = [{'Name': 'Movies', 'CollectionType': 'movies', 'ItemId': 'a', 'Locations': ['/media/Movies']},
            {'Name': 'Emby TV', 'CollectionType': 'tvshows', 'Id': 'b', 'Locations': ['/media/TV']},
            {'Name': 'Broken'}]
    monkeypatch.setattr(jd, '_request', lambda *a, **k: Resp(200, body))
    assert jd.get_libraries() == [
        {'id': 'a', 'name': 'Movies', 'type': 'movies', 'paths': ['/media/Movies']},
        {'id': 'b', 'name': 'Emby TV', 'type': 'tvshows', 'paths': ['/media/TV']}]


# --- library reads ---------------------------------------------------------------------------

def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S.1234567Z')


class FakeSession:
    def __init__(self, items, shows):
        self.items, self.shows, self.item_calls = items, shows, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith('/Users'):
            return Resp(200, [{'Id': 'u1', 'Policy': {'IsAdministrator': True}}])
        if params and params.get('Ids'):
            return Resp(200, {'Items': [s for s in self.shows if s['Id'] in params['Ids'].split(',')]})
        self.item_calls += 1
        start = params['StartIndex']
        return Resp(200, {'Items': self.items[start:start + params['Limit']]})


def test_recent_items_stop_at_the_cutoff_and_carry_show_ids(settings, monkeypatch):
    monkeypatch.setattr(jd, '_PAGE', 2)
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    items = [
        {'Type': 'Episode', 'Id': 'e1', 'Name': 'Pilot', 'SeriesId': 's1', 'ParentIndexNumber': 1, 'IndexNumber': 1,
         'DateCreated': _iso(1), 'Path': '/media/clid/TV Shows/Rel/s01e01.mkv'},
        {'Type': 'Movie', 'Id': 'm1', 'Name': 'Film', 'ProductionYear': 2020, 'ProviderIds': {'Imdb': 'tt1'},
         'DateCreated': _iso(2), 'Path': '/media/clid/Movies/Film/film.mkv'},
        {'Type': 'Movie', 'Id': 'm2', 'Name': 'Old', 'DateCreated': _iso(100), 'Path': '/media/clid/Movies/Old/o.mkv'},
        {'Type': 'Movie', 'Id': 'm3', 'Name': 'Older', 'DateCreated': _iso(200), 'Path': '/media/clid/Movies/X/x.mkv'},
    ]
    shows = [{'Id': 's1', 'Name': 'Show', 'ProductionYear': 2019, 'ProviderIds': {'Imdb': 'tt9', 'Tmdb': '9'}}]
    session = FakeSession(items, shows)
    monkeypatch.setattr(jd.requests, 'Session', lambda: session)
    lib = jd.get_recent_items(hours=48)
    assert [m['title'] for m in lib['movies']] == ['Film']
    (ep,) = lib['episodes']
    assert (ep['title'], ep['imdb_id'], ep['season_number'], ep['episode_number']) == ('Show', 'tt9', 1, 1)
    assert ep['location'] == '/mnt/clid/TV Shows/Rel/s01e01.mkv'
    assert session.item_calls == 2  # second page reached the cutoff; no third page


def test_collect_matches_checking_items_by_file_name(settings, monkeypatch):
    lib = {'movies': [{'location': '/mnt/clid/Movies/A/a.mkv', 'type': 'movie', 'title': 'A'},
                      {'location': '/mnt/clid/Movies/B/b.mkv', 'type': 'movie', 'title': 'B'}], 'episodes': []}
    conn = mock.MagicMock()
    conn.execute.return_value.fetchone.return_value = [0]
    with mock.patch.object(jd, 'get_recent_items', return_value=lib), \
            mock.patch('database.add_collected_items') as add, \
            mock.patch('database.core.get_db_connection', return_value=conn):
        assert jd.collect_from_jellyfin([{'id': 7, 'filled_by_file': 'A.MKV'}]) == (1, 1)
    assert [e['title'] for e in add.call_args[0][0]] == ['A']


def test_collect_reports_none_when_the_server_does_not_answer(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_recent_items', lambda **k: None)
    assert jd.collect_from_jellyfin([{'id': 1, 'filled_by_file': 'a.mkv'}]) is None


# --- routing in the rest of cli_debrid --------------------------------------------------------

def test_plex_scan_and_removal_go_to_jellyfin(settings, monkeypatch):
    import database  # noqa: F401  (app import order)
    from utilities.plex_functions import update, removal
    from utilities import silo_functions
    monkeypatch.setattr(silo_functions, 'get_setting', getter())
    monkeypatch.setattr(update, 'claim_plex_mode_scan', lambda folder: True)
    with mock.patch.object(jd, 'update_item', return_value=True) as up, \
            mock.patch.object(jd, 'remove_file', return_value=True) as rm, \
            mock.patch('plexapi.server.PlexServer', side_effect=AssertionError('no Plex')):
        assert update.plex_update_item({'full_path': '/mnt/clid/__all__/Rel/ep.mkv', 'type': 'episode'})
        assert removal.remove_file_from_plex('Show', '/mnt/clid/__all__/Rel/ep.mkv', 'Ep')
    up.assert_called_once()
    rm.assert_called_once_with('Show', '/mnt/clid/__all__/Rel/ep.mkv', 'Ep')


def test_plex_only_tasks_are_off_and_checking_stays_on():
    from queues.runner import task_registry as tr
    g = getter()
    assert tr._plex_mode(g) is False
    assert tr._watch_history_default(getter({('Debug', 'do_not_add_plex_watch_history_items_to_queue'): True})) is False
    assert tr._checking_default(g) is True


def test_deletion_manager_never_calls_plex(settings, monkeypatch):
    import database  # noqa: F401
    from utilities import deletion_manager as dm, silo_functions
    g = getter({('Plex', 'url'): 'http://old-plex:32400', ('Plex', 'token'): 'old'})
    for mod in (dm, silo_functions, direct_mount, jd):
        monkeypatch.setattr(mod, 'get_setting', g)
    manager = dm.DeletionManager()
    assert manager.plex_enabled is False and manager.jellyfin_direct_enabled is True
    with mock.patch.object(jd, 'remove_file', return_value=True) as rm:
        result = manager.remove_from_media_server({'title': 'Film', 'type': 'movie',
                                                   'location_on_disk': '/mnt/clid/Movies/Film/f.mkv'})
    assert result == {'success': True, 'server_type': 'jellyfin', 'error': None}
    rm.assert_called_once()


def test_provider_ids_read_whatever_the_key_case():
    import database  # noqa: F401
    from utilities.emby_functions import _extract_ids
    # Jellyfin: 'Imdb'/'Tmdb'; Emby: 'Imdb' on movies but 'IMDB' on series and episodes
    assert _extract_ids({'Imdb': 'tt1', 'Tmdb': '2'}) == ('tt1', '2')
    assert _extract_ids({'IMDB': 'tt0903747', 'Tmdb': '1396', 'Tvdb': '81189'}) == ('tt0903747', '1396')
    assert _extract_ids({'Tvdb': '1'}) == (None, None)
    assert _extract_ids(None) == (None, None)


def test_connection_names_jellyfin_and_emby(settings, monkeypatch):
    monkeypatch.setattr(jd, '_request', lambda *a, **k: Resp(200, {'ServerName': 'box', 'ProductName': 'Jellyfin Server', 'Version': '10.11'}))
    assert jd.test_connection() == (True, 'box (Jellyfin Server 10.11)')
    monkeypatch.setattr(jd, '_request', lambda *a, **k: Resp(200, {'ServerName': 'box', 'Version': '4.10.2.0'}))
    assert jd.test_connection() == (True, 'box (Emby 4.10.2.0)')
    monkeypatch.setattr(jd, '_request', lambda *a, **k: Resp(401, {}))
    assert jd.test_connection()[0] is False
