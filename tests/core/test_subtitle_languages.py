"""Languages other than English: codes from the setting, sidecar names,
Plex/Jellyfin/ffprobe tags all compare (en/eng/English, fre/fra/French,
pob/pt-BR, zt/zh-TW, es-419...), forced sidecars don't count, and the
'ignore embedded subtitles' setting."""
import os
import sqlite3
import tempfile
from unittest import mock

import pytest

pytest.importorskip('babelfish')

from utilities import subtitle_lang as L  # noqa: E402


@pytest.mark.parametrize('code,key', [
    ('en', 'eng'), ('eng', 'eng'), ('English', 'eng'), ('en-US', 'eng-US'),
    ('fre', 'fra'), ('fra', 'fra'), ('French', 'fra'), ('fr', 'fra'),
    ('ger', 'deu'), ('de', 'deu'), ('chi', 'zho'), ('zt', 'zho-TW'), ('zh-Hant', 'zho-TW'),
    ('pob', 'por-BR'), ('pt-BR', 'por-BR'), ('pt', 'por'), ('es-419', 'spa'), ('spa', 'spa'),
    ('cze', 'ces'), ('gre', 'ell'), ('rum', 'ron'), ('per', 'fas'), ('iw', 'heb'), ('nob', 'nob'),
    ('und', None), ('', None), ('forced', None),
])
def test_codes_normalise(code, key):
    assert L.key(code) == key


def test_country_rule():
    assert L.matches('en', 'en-US') and L.matches('fra', 'fre') and L.matches('pt', 'pob')
    assert not L.matches('pt-BR', 'pt')            # asked for Brazilian: plain Portuguese doesn't do
    assert L.matches('pt-BR', 'pob') and not L.matches('zho-TW', 'zho')


@pytest.mark.parametrize('middle,lang,flags', [
    ('en', 'eng', set()), ('eng.hi', 'eng', {'hi'}), ('English.forced', 'eng', {'forced'}),
    ('pt-BR', 'por-BR', set()), ('fr.sdh', 'fra', {'sdh'}), ('hi', 'hin', set()), ('forced', None, {'forced'}),
])
def test_sidecar_names(middle, lang, flags):
    assert L.sidecar_lang(middle) == (lang, flags)


def test_setting_codes_expand():
    pytest.importorskip('subliminal')
    from utilities import downsub
    got = {L.key(x) for x in downsub.expand_languages(['cze', 'gre', 'en', 'English', 'es-419', 'pob', 'zt'])}
    assert {'ces', 'ell', 'eng', 'spa', 'por-BR', 'zho-TW'} <= got


@pytest.fixture
def lib(monkeypatch):
    pytest.importorskip('subliminal')
    tmp = tempfile.mkdtemp(prefix='langs_')
    monkeypatch.setenv('USER_DB_CONTENT', tmp)
    import database  # noqa: F401
    from database import subtitle_history
    monkeypatch.setattr(subtitle_history, '_ready', False)
    d = os.path.join(tmp, 'lib')
    os.makedirs(d)
    files = {
        1: ['Film1.fr.srt'],                            # French by alpha2
        2: ['Film2.French.forced.srt'],                 # forced only: not French
        3: ['Film3.pt.srt'],                            # plain Portuguese: not pt-BR
        4: ['Film4.pt-BR.srt', 'Film4.fre.srt'],        # both
        5: [],
    }
    conn = sqlite3.connect(os.path.join(tmp, 'media_items.db'))
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, title TEXT, year INTEGER,
        state TEXT, type TEXT, episode_title TEXT, season_number INTEGER, episode_number INTEGER,
        collected_at TEXT, filled_by_file TEXT, location_on_disk TEXT, version TEXT, ms_item_id TEXT)""")
    for i, subs in files.items():
        video = os.path.join(d, f'Film{i}.mkv')
        open(video, 'w').close()
        for s in subs:
            open(os.path.join(d, s), 'w').close()
        conn.execute("INSERT INTO media_items VALUES (?, 'tt1', ?, 2020, 'Collected', 'movie', NULL, NULL, NULL, "
                     "'2026-11-01', ?, ?, '1080p', ?)", (i, f'Film{i}', f'Film{i}.mkv', video, str(700 + i)))
    conn.commit()
    conn.close()
    return tmp


def _status(extra=None, ignore=False):
    from utilities import subtitle_status, plex_subtitle_info as psi
    vals = {('File Management', 'file_collection_management'): 'Plex',
            ('Subtitle Settings', 'subtitle_languages'): 'fre,pob',
            ('Subtitle Settings', 'ignore_embedded_subtitles'): ignore,
            ('Plex', 'url'): 'http://plex', ('Plex', 'token'): 't'}
    get = lambda s, k, d=None: vals.get((s, k), d)  # noqa: E731
    with mock.patch('utilities.settings.get_setting', side_effect=get), \
            mock.patch('utilities.config.downsub_config.get_setting', side_effect=get):
        if extra:
            psi._store(extra)
        rows = {r['id']: {x['lang']: x['has'] for x in r['langs']}
                for r in subtitle_status.with_status(subtitle_status.collected())}
    return rows


def test_status_with_french_and_brazilian(lib):
    rows = _status()
    fr, br = 'fr', 'pt-BR'
    assert rows[1] == {fr: True, br: False}
    assert rows[2] == {fr: False, br: False}         # forced sidecar
    assert rows[3] == {fr: False, br: False}         # plain pt isn't pt-BR
    assert rows[4] == {fr: True, br: True}


def test_embedded_tracks_tagged_with_legacy_codes_count(lib):
    # Plex/Jellyfin report French as 'fre'; an image-based Brazilian track doesn't count
    tracks = {'705': [{'lang': 'fr', 'code': 'fre', 'codec': 'srt', 'forced': False},
                      {'lang': 'pt-BR', 'code': 'por', 'codec': 'pgs', 'forced': False}]}
    rows = _status(tracks)
    assert rows[5] == {'fr': True, 'pt-BR': False}


def test_ignore_embedded_setting(lib):
    tracks = {'705': [{'lang': 'fr', 'code': 'fre', 'codec': 'srt', 'forced': False}]}
    rows = _status(tracks, ignore=True)
    assert rows[5] == {'fr': False, 'pt-BR': False}


def test_download_skips_languages_embedded_under_legacy_tags(monkeypatch):
    pytest.importorskip('subliminal')
    from utilities import downsub
    monkeypatch.setattr(downsub.config, 'PROBE_FOR_EMBEDDED', True, raising=False)
    monkeypatch.setattr(downsub.config, 'IGNORE_EMBEDDED', False, raising=False)
    monkeypatch.setattr(downsub.config, 'SUBTITLE_LANGUAGES', ['fre', 'ger'], raising=False)
    with mock.patch.object(downsub, '_plex_embedded_langs', return_value={'fra'}), \
            mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: 'Plex' if k == 'file_collection_management' else d), \
            mock.patch.object(downsub, '_compute_hash', return_value=(None, None)):
        ctx = downsub._prepare_video('/m/Film.2020.1080p.BluRay.x264-GRP.mkv', None, None,
                                     {'id': 1, 'type': 'movie', 'title': 'Film', 'year': 2020})
    assert [L.key(x) for x in ctx['languages']] == ['deu']        # French was embedded


def test_forced_flag_on_a_full_track():
    """Anime releases often flag the full dialogue track forced (Dragon Ball:
    'Stylized Subtitles [iKaos]', forced). It still covers English; a real
    forced/signs track doesn't, so a really missing language is still fetched."""
    from utilities.subtitle_lang import counts_as_full, track_frames
    assert counts_as_full({'forced': False})
    assert counts_as_full({'forced': True, 'title': 'Stylized Subtitles [iKaos/corre]'})
    assert counts_as_full({'forced': True, 'title': 'English', 'frames': 412})
    assert counts_as_full({'forced': True, 'title': 'Full Subs'})
    assert not counts_as_full({'forced': True, 'title': 'Signs/Songs [iKaos]', 'frames': 400})
    assert not counts_as_full({'forced': True, 'title': 'English Forced'})
    assert not counts_as_full({'forced': True, 'title': '', 'frames': 37})
    assert not counts_as_full({'forced': True})                     # nothing to go on: still forced
    assert track_frames({'NUMBER_OF_FRAMES-eng': '512'}) == 512
    assert track_frames({'language': 'eng'}) is None


def test_embedded_languages_use_the_rule():
    from unittest import mock
    from utilities import downsub
    signs_only = [{'codec': 'ass', 'language': 'eng', 'forced': True, 'title': 'Signs/Songs', 'frames': 60}]
    full = signs_only + [{'codec': 'ass', 'language': 'eng', 'forced': True, 'title': 'Stylized Subtitles', 'frames': 380}]
    with mock.patch.object(downsub, '_embedded_tracks', return_value=signs_only):
        assert downsub.get_embedded_subtitle_languages('/x.mkv', text_only=False) == set()
    with mock.patch.object(downsub, '_embedded_tracks', return_value=full):
        assert downsub.get_embedded_subtitle_languages('/x.mkv', text_only=False) == {'eng'}


def test_plex_tracks_use_the_rule():
    from utilities.plex_subtitle_info import good_embedded_langs
    tracks = [{'lang': 'en', 'code': 'eng', 'codec': 'srt', 'forced': True, 'title': 'English Full'},
              {'lang': 'es', 'code': 'spa', 'codec': 'srt', 'forced': True, 'title': 'Forced'}]
    assert good_embedded_langs(tracks) == {'eng'}
