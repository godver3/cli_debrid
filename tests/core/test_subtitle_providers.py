"""SubDL / SubSource providers, release-name ranking, SRT tag stripping."""
import io
import zipfile
from unittest import mock

import pytest

pytest.importorskip('subliminal')

from babelfish import Language  # noqa: E402
from subliminal.video import Episode, Movie  # noqa: E402

from utilities import subtitle_providers as sp  # noqa: E402
from utilities import downsub  # noqa: E402


class _R:
    def __init__(self, data=None, content=b'', status=200):
        self._d, self.content, self.status_code = data, content, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _ep():
    v = Episode('Show.S01E02.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv', 'Show', 1, 2,
                release_group='NTb', source='Web', resolution='1080p', streaming_service='Amazon Prime')
    v.external_ids['series_imdb_id'] = 'tt0000001'
    return v


def test_subdl_lists_only_the_right_episode_and_matches_release():
    data = {'status': True, 'subtitles': [
        {'language': 'EN', 'downloads': 50, 'release_name': 'Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb',
         'url': '/subtitle/a.zip'},
        {'language': 'EN', 'downloads': 900, 'release_name': 'Show.S01E03.1080p.WEB-DL-NTb', 'url': '/subtitle/b.zip'},
    ]}
    p = sp.SubdlProvider(api_key='k')
    p.initialize()
    with mock.patch.object(p.session, 'get', return_value=_R(data)) as get:
        subs = p.list_subtitles(_ep(), {Language('eng')})
    assert get.call_args.kwargs['params']['imdb_id'] == 'tt0000001'
    assert [s.release_name for s in subs] == ['Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb']
    m = subs[0].get_matches(_ep())
    assert {'series', 'season', 'episode', 'release_group', 'source'} <= m
    assert subs[0].download_count == 50


def test_subdl_without_key_lists_nothing():
    p = sp.SubdlProvider(api_key='')
    p.initialize()
    assert p.list_subtitles(_ep(), {Language('eng')}) == []


def test_subdl_download_picks_episode_from_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('Show.S01E01.srt', 'one')
        z.writestr('Show.S01E02.srt', 'two')
        z.writestr('Show.S01E02.ass', 'two-ass')
    p = sp.SubdlProvider(api_key='k')
    p.initialize()
    sub = sp.SubdlSubtitle(Language('eng'), 'u', season=1, episode=2, download={'url': 'https://dl/x.zip'})
    with mock.patch.object(p.session, 'get', return_value=_R(content=buf.getvalue())):
        p.download_subtitle(sub)
    assert sub.content == b'two'


def test_subsource_search_and_season_filter():
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        if url.endswith('/movies/search'):
            return _R({'data': [{'movieId': 7, 'season': 1}, {'movieId': 8, 'season': 2}]})
        return _R({'data': [{'subtitleId': 99, 'releaseInfo': ['Show.S01E02.1080p.WEB-DL-NTb'], 'downloads': 3,
                             'language': 'english'},
                            {'subtitleId': 100, 'releaseInfo': ['Show.S01E05.720p'], 'language': 'english'}]})
    p = sp.SubsourceProvider(api_key='k')
    p.initialize()
    with mock.patch.object(p.session, 'get', side_effect=get):
        subs = p.list_subtitles(_ep(), {Language('eng')})
    assert [s.id for s in subs] == ['99']
    assert any(u.endswith('/subtitles') and pr['movieId'] == 7 for u, pr in calls)
    assert not any(u.endswith('/subtitles') and pr['movieId'] == 8 for u, pr in calls)


def test_movie_imdb_match_counts_as_title():
    v = Movie('Dune.2021.2160p.WEB-DL.mkv', 'Dune', year=2021, source='Web', resolution='2160p')
    v.external_ids['imdb_id'] = 'tt1160419'
    s = sp.SubdlSubtitle(Language('eng'), 'u', release_name='Dune.2021.2160p.WEB-DL.DDP5.1.Atmos-FLUX', year=2021)
    assert {'title', 'year', 'source', 'resolution'} <= s.get_matches(v)


def test_release_similarity_prefers_closer_release():
    target = 'Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb'
    close = downsub.release_similarity(target, 'Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb')
    far = downsub.release_similarity(target, 'Show.S01E02.720p.HDTV.x264-KILLERS')
    assert close > far


def test_ranker_breaks_ties_by_release_then_downloads():
    v = _ep()

    class S:
        provider_name = 'x'

        def __init__(self, rel, dl):
            self.release_name, self.download_count = rel, dl

        def get_matches(self, video):
            return {'series', 'season', 'episode'}
    with mock.patch('subliminal.score.compute_score', return_value=900):
        rank = downsub._ranker('Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb')
        a = rank(S('Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb', 1), v)
        b = rank(S('Show.S01E02.720p.HDTV', 5000), v)
        c = rank(S('Show.S01E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb', 500), v)
    assert c > a > b and a // 10000 == 900


def test_override_tags_stripped_in_text_only_mode(monkeypatch):
    class S:
        subtitle_format = 'srt'
        content = b'1\n00:00:01,000 --> 00:00:02,000\n{\\an8}{\\pos(273,402)}Hello\n'
    monkeypatch.setattr(downsub.config, 'TEXT_SUBTITLES_ONLY', True, raising=False)
    s = S()
    assert downsub._final_format(s) == ('srt', '.srt')
    assert b'{\\' not in s.content and b'Hello' in s.content


def test_providers_registered_and_configured(monkeypatch):
    from subliminal.extensions import provider_manager
    assert {'subdl', 'subsource'} <= set(provider_manager.names())
    monkeypatch.setattr(downsub.config, 'SUBDL_API_KEY', 'abc', raising=False)
    monkeypatch.setattr(downsub.config, 'SUBSOURCE_API_KEY', '', raising=False)
    pc = downsub.build_provider_configs()
    assert pc['subdl'] == {'api_key': 'abc'} and 'subsource' not in pc


def test_episode_in_understands_ep_numbers():
    assert sp.episode_in('Dragon ball 1986 Ep6 Keep an eye on the Dragon balls', 1, 132) is False
    assert sp.episode_in('Dragon Ball Episode 132', 1, 132) is True
    assert sp.episode_in('Dragon.Ball.S01E132', 1, 132) is True
    assert sp.episode_in('Dragon Ball 1986 960p BluRay DD5.1 x264', 1, 132) is None   # x264 is not an episode


def test_subsource_release_without_episode_is_not_this_episode():
    """Live 2026-11-22: SubSource listed every Dragon Ball subtitle (episodes
    4, 5, 6, a movie) and each one matched S01E132 on season + episode."""
    def get(url, params=None, headers=None, timeout=None):
        if url.endswith('/movies/search'):
            return _R({'data': [{'movieId': 7}]})
        return _R({'data': [{'subtitleId': 1, 'releaseInfo': ['Dragon ball 1986 Ep6 Keep an eye'], 'language': 'english'},
                            {'subtitleId': 2, 'releaseInfo': ['Dragon Ball Curse of the Blood Rubies'], 'language': 'english'},
                            {'subtitleId': 3, 'releaseInfo': ['Dragon Ball S01 Complete 1080p'], 'language': 'english'},
                            {'subtitleId': 4, 'releaseInfo': ['Dragon Ball Episode 132'], 'language': 'english'}]})
    v = Episode('Dragon Ball S01E132.mkv', 'Dragon Ball', 1, 132)
    v.external_ids['series_imdb_id'] = 'tt0088509'
    p = sp.SubsourceProvider(api_key='k')
    p.initialize()
    with mock.patch.object(p.session, 'get', side_effect=get):
        subs = {s.id: s for s in p.list_subtitles(v, {Language('eng')})}
    assert '1' not in subs                                     # names episode 6
    assert (subs['2'].season, subs['2'].episode) == (None, None)   # names no episode: identity unknown
    assert {'season', 'episode'} - subs['2'].get_matches(v) == {'season', 'episode'}
    assert (subs['3'].season, subs['3'].episode) == (1, 132)       # this season's pack
    assert {'season', 'episode'} <= subs['4'].get_matches(v)


def test_pack_zip_needs_a_file_naming_the_episode():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('Dragon Ball 001.srt', 'one')
        z.writestr('Dragon Ball 002.srt', 'two')
    assert sp.pick_from_zip(buf.getvalue(), 1, 132) == b''       # no file is episode 132: nothing
    single = io.BytesIO()
    with zipfile.ZipFile(single, 'w') as z:
        z.writestr('subtitle.srt', 'only')
    assert sp.pick_from_zip(single.getvalue(), 1, 132) == b'only'   # a lone file is the subtitle


def test_opensubtitles_org_terminate_closes_locally_not_as_remote_call():
    from subliminal.providers.opensubtitles import OpenSubtitlesProvider
    from utilities.subtitle_providers import _patch_opensubtitles_org_terminate

    _patch_opensubtitles_org_terminate()
    calls = []

    class FakeServer:
        def LogOut(self, token):
            calls.append(('LogOut', token))
            return {'status': '200 OK'}

        def __getattr__(self, name):  # any other remote method, e.g. 'close'
            raise AssertionError(f'remote call {name}')

        def __call__(self, attr):
            assert attr == 'close'
            return lambda: calls.append(('local close',))

    prov = OpenSubtitlesProvider.__new__(OpenSubtitlesProvider)
    prov.server, prov.token = FakeServer(), 'tok'
    prov.terminate()
    assert calls == [('LogOut', 'tok'), ('local close',)]
    assert prov.token is None


def test_gestdown_show_not_found_logs_as_info(caplog):
    import logging
    from utilities.subtitle_providers import _quiet_gestdown_misses

    _quiet_gestdown_misses()
    log = logging.getLogger('subliminal.providers.gestdown')
    with caplog.at_level(logging.INFO, logger='subliminal.providers.gestdown'):
        log.error('No show id found for %r', 'Pokemon')
        log.error('Something else broke')
    levels = {r.getMessage(): r.levelname for r in caplog.records}
    assert levels["No show id found for 'Pokemon'"] == 'INFO'
    assert levels['Something else broke'] == 'ERROR'
