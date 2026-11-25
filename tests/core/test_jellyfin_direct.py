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


@pytest.fixture(autouse=True)
def _fresh_lookup_caches():
    from utilities import silo_functions as silo
    jd._id_map.update(at=0.0, map=None)
    silo._file_id_map.update(at=0.0, map=None)
    yield
    jd._id_map.update(at=0.0, map=None)
    silo._file_id_map.update(at=0.0, map=None)


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


# --- Symlinked/Local's library read (get_collected_from_emby) ---------------------------------

class LibSession(FakeSession):
    """Answers like current Jellyfin: libraries only from /Library/VirtualFolders."""
    def __init__(self, virtual_folders, items_by_parent):
        super().__init__([], [])
        self.vf, self.by_parent, self.user_param = virtual_folders, items_by_parent, None

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith('/Users'):
            return Resp(200, [{'Id': 'viewer', 'Policy': {'IsAdministrator': False}},
                              {'Id': 'admin', 'Policy': {'IsAdministrator': True}}])
        if url.endswith('/Library/VirtualFolders'):
            return Resp(200, self.vf)
        self.user_param = url.split('/Users/')[1].split('/')[0]
        if params.get('IncludeItemTypes') == 'CollectionFolder':
            return Resp(200, {'Items': []})  # what current Jellyfin answers
        return Resp(200, {'Items': self.by_parent.get(params.get('ParentId'), [])})


def test_symlink_mode_library_read_finds_libraries_on_current_jellyfin(monkeypatch):
    import database  # noqa: F401
    from utilities import emby_functions as ef
    monkeypatch.setattr(ef, 'get_setting', getter())
    vf = [{'Name': 'Films', 'CollectionType': 'movies', 'ItemId': 'L1', 'Locations': ['/m']},
          {'Name': 'Series', 'CollectionType': 'tvshows', 'ItemId': 'L2', 'Locations': ['/t']},
          {'Name': 'Music', 'CollectionType': 'music', 'ItemId': 'L3', 'Locations': ['/a']}]
    movie = {'Type': 'Movie', 'Id': 'm', 'Name': 'Film', 'ProviderIds': {'Imdb': 'tt1'}, 'Path': '/m/F/f.mkv'}
    show = {'Type': 'Series', 'Id': 's', 'Name': 'Show', 'ProviderIds': {'IMDB': 'tt9'}}
    ep = {'Type': 'Episode', 'Id': 'e', 'Name': 'Pilot', 'ParentIndexNumber': 1, 'IndexNumber': 1, 'Path': '/t/S/e.mkv'}
    session = LibSession(vf, {'L1': [movie], 'L2': [show], 's': [ep]})
    monkeypatch.setattr(ef.requests, 'Session', lambda: session)
    lib = ef.get_collected_from_emby(bypass=True)
    assert [m['imdb_id'] for m in lib['movies']] == ['tt1']
    assert [(e['title'], e['imdb_id']) for e in lib['episodes']] == [('Show', 'tt9')]
    assert session.user_param == 'admin'  # an administrator, not the first listed user


# --- subtitles in Direct Mount ----------------------------------------------------------------------

class _Lang:
    ietf, alpha3 = 'en', 'eng'

    def __str__(self):
        return 'en'


class _Sub:
    language = _Lang()
    content = b'1\n00:00:01,000 --> 00:00:02,000\nhi\n'


def _ctx(tmp_path, name='Rel.mkv'):
    from pathlib import Path
    video = mock.Mock()
    return {'original_path': Path(tmp_path) / name, 'rating_key': None, 'video': video,
            'item': {'id': 1, 'type': 'movie', 'title': 'Film'}}


@pytest.fixture
def downsub(settings, monkeypatch):
    import database  # noqa: F401
    from utilities import downsub as ds
    monkeypatch.setattr(ds, '_final_format', lambda sub: ('srt', '.srt'))
    monkeypatch.setattr(ds, '_mark_release_match', lambda *a: None)
    monkeypatch.setattr(ds, '_record_history', lambda ctx, sub, lang, fmt, via, path, source: ctx.setdefault('via', []).append(via))
    return ds


def test_writable_folder_gets_a_sidecar_and_a_rescan(downsub, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    with mock.patch.object(jd, 'pick_up_sidecar', return_value=True) as rescan, \
            mock.patch.object(jd, 'upload_subtitle') as up:
        assert downsub.save_subtitles(ctx, [_Sub()]) == 1
    assert (tmp_path / 'Rel.en.srt').read_bytes() == _Sub.content
    assert ctx['via'] == ['sidecar']
    rescan.assert_called_once()
    up.assert_not_called()


def test_read_only_folder_uploads_through_the_api(downsub, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(downsub.os, 'access', lambda p, mode: False)
    with mock.patch.object(jd, 'find_item_id', return_value='abc') as find, \
            mock.patch.object(jd, 'upload_subtitle', return_value=True) as up, \
            mock.patch.object(jd, 'pick_up_sidecar') as rescan:
        assert downsub.save_subtitles(ctx, [_Sub(), _Sub()]) == 2
    find.assert_called_once()  # looked up once per file, not per subtitle
    assert up.call_args.kwargs['item_id'] == 'abc' and up.call_args.args[2] == 'eng'
    assert ctx['via'] == ['jellyfin', 'jellyfin']
    rescan.assert_not_called()


def test_failed_upload_falls_back_to_cli_mount_injection(downsub, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(downsub.os, 'access', lambda p, mode: False)
    client = mock.Mock()
    client.inject_sidecar_file.return_value = True
    with mock.patch.object(jd, 'find_item_id', return_value=None), \
            mock.patch.object(jd, 'pick_up_sidecar', return_value=True) as rescan, \
            mock.patch('utilities.builtin_engine.selected', return_value=False), \
            mock.patch('usenet.climount_client.get_climount_client', return_value=client):
        assert downsub.save_subtitles(ctx, [_Sub()]) == 1
    assert ctx['via'] == ['cli_mount']
    rescan.assert_called_once()


def test_upload_subtitle_posts_base64(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append((m, p, k)) or Resp(204))
    assert jd.upload_subtitle('/mnt/clid/Movies/R/r.mkv', b'abc', 'eng', 'srt', item_id='42')
    (m, p, k), = calls
    assert (m, p) == ('POST', '/Videos/42/Subtitles')
    assert k['json'] == {'Language': 'eng', 'Format': 'srt', 'IsForced': False, 'IsHearingImpaired': False, 'Data': 'YWJj'}


# --- Silo subtitles in Direct Mount ------------------------------------------------------------------

SILO = {('File Management', 'plex_mode_media_server'): 'silo',
        ('Debug', 'silo_url'): 'http://silo:8090', ('Debug', 'silo_api_key'): 'sa_key'}


def test_silo_upload_is_a_multipart_form_as_an_admin_profile(monkeypatch):
    from utilities import silo_functions as silo
    monkeypatch.setattr(silo, 'get_setting', getter(SILO))
    silo._profile_cache.update(key=None, id=None)
    monkeypatch.setattr(silo, 'primary_profile_id', lambda url, key: 'prof1')
    calls = []
    monkeypatch.setattr(silo, '_request', lambda m, p, *a, **k: calls.append((m, p, k)) or Resp(200, {'subtitle': {}}))
    assert silo.upload_subtitle('/mnt/clid/Movies/R/Rel.mkv', b'abc', 'eng', 'srt', file_id='f9', release_name='Rel.WEB')
    (m, p, k), = calls
    assert (m, p) == ('POST', '/api/v2/subtitles/upload')
    assert k['headers_extra'] == {'X-Profile-Id': 'prof1'}
    assert k['data'] == {'media_file_id': 'f9', 'language': 'eng', 'release_name': 'Rel.WEB'}
    assert k['files']['file'][:2] == ('Rel.eng.srt', b'abc')


def test_silo_file_id_comes_from_the_catalog(monkeypatch):
    from utilities import silo_functions as silo
    lib = {'movies': [{'location': '/mnt/clid/Movies/R/Rel.mkv', 'media_file_id': 'f9'}], 'episodes': []}
    monkeypatch.setattr(silo, 'get_recent_from_silo', lambda hours=48: {'movies': [], 'episodes': []})
    monkeypatch.setattr(silo, 'get_collected_from_silo', lambda added_after=None: lib)
    assert silo.find_file_id('/other/view/REL.MKV') == 'f9'
    assert silo.find_file_id('/x/missing.mkv') is None


def test_read_only_folder_uploads_to_silo(downsub, tmp_path, monkeypatch):
    from utilities import silo_functions as silo
    g = getter(SILO)
    for mod in (direct_mount, silo):
        monkeypatch.setattr(mod, 'get_setting', g)
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(downsub.os, 'access', lambda p, mode: False)
    with mock.patch.object(silo, 'find_file_id', return_value='f9'), \
            mock.patch.object(silo, 'upload_subtitle', return_value=True) as up, \
            mock.patch.object(jd, 'upload_subtitle') as jf_up:
        assert downsub.save_subtitles(ctx, [_Sub()]) == 1
    assert up.call_args.kwargs['file_id'] == 'f9'
    assert ctx['via'] == ['silo']
    jf_up.assert_not_called()


def test_plex_direct_mount_still_uploads_to_plex(downsub, tmp_path, monkeypatch):
    from utilities import silo_functions as silo
    g = getter({('File Management', 'plex_mode_media_server'): 'plex'})
    for mod in (direct_mount, silo, jd):
        monkeypatch.setattr(mod, 'get_setting', g)
    ctx = dict(_ctx(tmp_path), rating_key='77')
    monkeypatch.setattr(downsub.os, 'access', lambda p, mode: False)
    with mock.patch.object(downsub, 'upload_subtitle_to_plex', return_value=True) as plex_up:
        assert downsub.save_subtitles(ctx, [_Sub()]) == 1
    assert plex_up.call_args.args[0] == '77' and ctx['via'] == ['plex']


def test_sidecar_pickup_rescans_and_refreshes_the_item(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(jd, 'scan_path', lambda p, t='': calls.append(('scan', p)) or True)
    monkeypatch.setattr(jd, 'find_item_id', lambda p: '120')
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append((m, p, k['params'])) or Resp(204))
    assert jd.pick_up_sidecar('/mnt/clid/TV Shows/R/e.mkv', 'episode')
    assert calls[0] == ('scan', '/mnt/clid/TV Shows/R/e.mkv')
    method, path, params = calls[1]
    assert (method, path) == ('POST', '/Items/120/Refresh')
    assert params['ImageRefreshMode'] == 'Default'  # Emby answers 400 to 'None'


@pytest.mark.parametrize('status,expect', [
    (404, 'takes no subtitle uploads (Emby)'),
    (500, 'Save subtitles into media folders'),
])
def test_refused_uploads_say_why(settings, monkeypatch, caplog, status, expect):
    r = Resp(status)
    r.text = ''
    monkeypatch.setattr(jd, '_request', lambda *a, **k: r)
    with caplog.at_level('WARNING'):
        assert jd.upload_subtitle('/mnt/x/a.mkv', b'abc', 'eng', item_id='1') is False
    assert expect in caplog.text


# --- review fixes 2026-11-24 ------------------------------------------------------------------

@pytest.mark.parametrize('local,expected', [
    # a release folder full of dots is a folder, not a file with extension ".x264-GRP"
    ('/mnt/clid/TV Shows/Show.S01E01.1080p.WEB-DL.x264-GRP', '/media/clid/TV Shows/Show.S01E01.1080p.WEB-DL.x264-GRP'),
    ('/mnt/clid/TV Shows/Show Name (2020)', '/media/clid/TV Shows/Show Name (2020)'),
    ('/mnt/clid/TV Shows/Show.S01E01.1080p.WEB-DL.x264-GRP/ep.mkv', '/media/clid/TV Shows/Show.S01E01.1080p.WEB-DL.x264-GRP'),
])
def test_scan_of_a_gone_path_goes_to_its_release_folder_not_the_library(settings, monkeypatch, local, expected):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    calls = []
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append(k['json']) or Resp(204))
    assert jd.scan_path(local, 'episode', removed=True)
    assert calls[0]['Updates'][0]['Path'] == expected


def test_scan_asks_the_filesystem_whether_the_path_is_a_folder(settings, tmp_path, monkeypatch):
    base = tmp_path / 'clid'
    rel = base / 'Movies' / 'Movie.2020.1080p.BluRay.x264-GRP'
    rel.mkdir(parents=True)
    (rel / 'movie.mkv').write_bytes(b'')
    libs = [{'id': 'm', 'name': 'Movies', 'type': 'movies', 'paths': ['/media/Movies']}]
    settings({('Plex', 'mounted_file_location'): f'{base}/__all__'})
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: libs)
    calls = []
    monkeypatch.setattr(jd, '_request', lambda m, p, *a, **k: calls.append(k['json']['Updates'][0]['Path']) or Resp(204))
    jd.scan_path(str(rel), 'movie')
    jd.scan_path(str(rel / 'movie.mkv'), 'movie')
    assert calls == ['/media/Movies/Movie.2020.1080p.BluRay.x264-GRP'] * 2


def test_every_version_of_an_item_is_read(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    items = [{'Type': 'Movie', 'Id': 'm1', 'Name': 'Film', 'DateCreated': _iso(1),
              'MediaSources': [{'Path': '/media/clid/Movies/A/film.1080p.mkv'},
                               {'Path': '/media/clid/Movies/B/film.2160p.mkv'},
                               {'Path': '/media/clid/Movies/B/film.2160p.mkv'}]}]
    session = FakeSession(items, [])
    monkeypatch.setattr(jd.requests, 'Session', lambda: session)
    lib = jd.get_all_items()
    assert sorted(m['location'] for m in lib['movies']) == [
        '/mnt/clid/Movies/A/film.1080p.mkv', '/mnt/clid/Movies/B/film.2160p.mkv']
    assert {m['ratingKey'] for m in lib['movies']} == {'m1'}


def test_library_read_leaves_out_missing_episodes(settings, monkeypatch):
    monkeypatch.setattr(jd, 'get_libraries', lambda *a, **k: LIBS)
    seen = []

    class S(FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            if params and 'StartIndex' in params:
                seen.append(params)
            return super().get(url, headers, params, timeout)
    monkeypatch.setattr(jd.requests, 'Session', lambda: S([], []))
    jd.get_all_items()
    assert seen and all(p['IsMissing'] == 'false' for p in seen)


def test_find_item_id_reuses_the_whole_library_read(settings, monkeypatch):
    reads = []
    full = {'movies': [{'location': '/mnt/clid/Movies/A/old.mkv', 'ratingKey': 'i1'}], 'episodes': []}
    monkeypatch.setattr(jd, 'get_recent_items', lambda hours=48: {'movies': [], 'episodes': []})
    monkeypatch.setattr(jd, 'get_all_items', lambda: reads.append(1) or full)
    assert jd.find_item_id('/x/OLD.mkv') == 'i1'
    assert jd.find_item_id('/x/other.mkv') is None
    assert jd.find_item_id('/x/old.mkv') == 'i1'
    assert len(reads) == 1


def test_silo_find_file_id_reuses_the_whole_catalog_read(monkeypatch):
    from utilities import silo_functions as silo
    reads = []
    lib = {'movies': [{'location': '/mnt/clid/Movies/R/Rel.mkv', 'media_file_id': 'f9'}], 'episodes': []}
    monkeypatch.setattr(silo, 'get_recent_from_silo', lambda hours=48: {'movies': [], 'episodes': []})
    monkeypatch.setattr(silo, 'get_collected_from_silo', lambda added_after=None: reads.append(1) or lib)
    assert silo.find_file_id('/a/rel.mkv') == 'f9'
    assert silo.find_file_id('/a/nope.mkv') is None
    assert len(reads) == 1


@pytest.mark.parametrize('overrides,expected', [
    ({}, None),
    ({('Debug', 'emby_jellyfin_token'): ''}, 'Jellyfin/Emby'),
    ({('File Management', 'plex_mode_media_server'): 'silo'}, 'Silo'),
    ({('File Management', 'plex_mode_media_server'): 'silo', ('Debug', 'silo_url'): 'http://s',
      ('Debug', 'silo_api_key'): 'k'}, None),
    ({('File Management', 'plex_mode_media_server'): 'plex', ('Debug', 'emby_jellyfin_token'): ''}, None),
])
def test_unconfigured_server(overrides, expected):
    assert direct_mount.unconfigured_server(getter(overrides)) == expected


def test_unconfigured_jellyfin_never_falls_through_to_plex(settings, monkeypatch):
    import database  # noqa: F401  (app import order)
    from utilities.plex_functions import update, removal, scan_cleanup
    from utilities import silo_functions as silo
    g = settings({('Debug', 'emby_jellyfin_token'): '',
                  ('Plex', 'url'): 'http://old-plex', ('Plex', 'token'): 't'})
    monkeypatch.setattr(silo, 'get_setting', g)
    for mod in (update, removal, scan_cleanup):
        monkeypatch.setattr(mod, 'get_setting', g, raising=False)
    with mock.patch('requests.get') as get, mock.patch('requests.post') as post, \
            mock.patch('plexapi.server.PlexServer') as plex:
        assert update.plex_update_item({'title': 'X', 'location_on_disk': '/mnt/clid/Movies/X/x.mkv'}) is False
        assert removal.remove_file_from_plex('X', '/mnt/clid/Movies/X/x.mkv') is False
        assert scan_cleanup.scan_and_empty_plex_trash(['/mnt/clid/Movies/X'], 'movie')['success'] is False
    get.assert_not_called(); post.assert_not_called(); plex.assert_not_called()
