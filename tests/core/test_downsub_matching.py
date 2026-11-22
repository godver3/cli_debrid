"""Subtitle matching: release-name choice, DB identity, score floor, formats."""
from pathlib import Path

import pytest

downsub = pytest.importorskip('utilities.downsub')
if not downsub.SUBLIMINAL_AVAILABLE:
    pytest.skip('subliminal not installed', allow_module_level=True)

from subliminal.video import Episode, Movie  # noqa: E402


def _ep_item(**kw):
    item = {'type': 'episode', 'title': 'Mare of Easttown', 'imdb_id': 'tt10155688',
            'tmdb_id': 115004, 'season_number': 1, 'episode_number': 3}
    item.update(kw)
    return item


def test_obfuscated_file_uses_release_name_and_db_identity():
    video, parse = downsub._build_video(
        Path('/mnt/x/tGcr.mkv'), None, None,
        _ep_item(filled_by_title='Mare.of.Easttown.S01E03.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb',
                 filled_by_file='tGcr.mkv'))
    assert isinstance(video, Episode)
    assert parse.startswith('Mare.of.Easttown.S01E03')
    assert video.release_group == 'NTb'
    assert video.streaming_service == 'Amazon Prime'
    assert (video.season, video.episode) == (1, 3)
    assert video.external_ids['series_imdb_id'] == 'tt10155688'
    assert video.external_ids['series_tmdb_id'] == '115004'


def test_season_pack_name_fills_group_for_episode_file():
    # Templated symlink name has the episode but no group; the pack name has
    # the group but no episode. Both are used.
    video, parse = downsub._build_video(
        Path('/lib/Mare of Easttown (2021)/Season 01/Mare of Easttown - S01E03 - Enter Number.mkv'),
        None, None,
        _ep_item(filled_by_title='Mare.of.Easttown.S01.1080p.BluRay.x264-ROVERS'))
    assert isinstance(video, Episode)
    assert video.release_group == 'ROVERS'
    assert video.source == 'Blu-ray'
    assert video.episode == 3


def test_db_episode_number_wins_over_parsed():
    video, _ = downsub._build_video(
        Path('/x/Show.S01E01.mkv'), None, 'Show.S01E01.720p.WEB-GRP',
        _ep_item(title='Show', season_number=2, episode_number=5))
    assert (video.season, video.episode) == (2, 5)


def test_unparseable_episode_built_from_db():
    video, _ = downsub._build_video(Path('/m/abc123.mkv'), None, None, _ep_item())
    assert isinstance(video, Episode)
    assert video.series == 'Mare of Easttown' and video.episode == 3


def test_movie_identity_and_score_floor():
    video, _ = downsub._build_video(
        Path('/m/a8f3c9d2e1b4a7c6.mkv'), None, None,
        {'type': 'movie', 'title': 'Dune', 'year': 2021, 'imdb_id': 'tt1160419',
         'filled_by_title': 'Dune.2021.2160p.MA.WEB-DL.DDP5.1.Atmos.HDR.HEVC-CMRG'})
    assert isinstance(video, Movie)
    assert video.year == 2021 and video.external_ids['imdb_id'] == 'tt1160419'
    assert video.release_group == 'CMRG'
    assert downsub._min_score(video) == downsub.MIN_MOVIE_SCORE
    # hash alone (movie hash score 323) clears the floor
    assert 323 >= downsub._min_score(video)


def test_episode_score_floor_needs_series_season_episode():
    v = Episode('x', 'Show', 1, 1)
    assert downsub._min_score(v) == 486 + 54 + 54
    assert 971 >= downsub._min_score(v)  # hash only


@pytest.mark.parametrize('name,obf', [
    ('tGcr.mkv', True), ('a8f3c9d2e1b4a7c6d5e4.mkv', True),
    ('Show.S01E01.mkv', False), ('Mare of Easttown - S01E03.mkv', False),
])
def test_looks_obfuscated(name, obf):
    assert downsub._looks_obfuscated(name) is obf


ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}Hello there
"""


def _ass_sub():
    from subliminal.subtitle import Subtitle
    from babelfish import Language
    s = Subtitle(Language('eng'), subtitle_format='ass')
    s.content = ASS.encode('utf-8')
    assert s.is_valid()
    return s


def test_ass_kept_with_real_extension_by_default(monkeypatch):
    monkeypatch.setattr(downsub.config, 'TEXT_SUBTITLES_ONLY', False, raising=False)
    assert downsub._final_format(_ass_sub()) == ('ass', '.ass')


def test_ass_converted_to_srt_in_text_only_mode(monkeypatch):
    monkeypatch.setattr(downsub.config, 'TEXT_SUBTITLES_ONLY', True, raising=False)
    sub = _ass_sub()
    assert downsub._final_format(sub) == ('srt', '.srt')
    text = sub.content.decode('utf-8')
    assert 'Hello there' in text and '{\\an8}' not in text and '00:00:01,000' in text


def test_embedded_probe_skips_forced_and_image_tracks(monkeypatch):
    import json
    import subprocess

    streams = {'streams': [
        {'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'eng'}, 'disposition': {'forced': 0}},
        {'codec_name': 'subrip', 'tags': {'language': 'spa'}, 'disposition': {'forced': 1}},
        {'codec_name': 'subrip', 'tags': {'language': 'fre'}, 'disposition': {'forced': 0}},
        {'codec_name': 'ass', 'tags': {'language': 'jpn'}, 'disposition': {'forced': 0}},
    ]}

    class R:
        returncode = 0
        stdout = json.dumps(streams)

    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: R())
    assert downsub.get_embedded_subtitle_languages('/x.mkv', text_only=False) == {'eng', 'fre', 'jpn'}
    assert downsub.get_embedded_subtitle_languages('/x.mkv', text_only=True) == {'fre'}


def test_selected_providers_honours_setting(monkeypatch):
    monkeypatch.setattr(downsub.config, 'SUBTITLE_PROVIDERS', ['podnapisi', 'bogus', 'gestdown'])
    assert downsub.selected_providers() == ['podnapisi', 'gestdown']
    monkeypatch.setattr(downsub.config, 'SUBTITLE_PROVIDERS', [])
    assert downsub.selected_providers() == downsub.DEFAULT_PROVIDERS


def test_oscom_gets_credentials(monkeypatch):
    monkeypatch.setattr(downsub.config, 'OPENSUBTITLES_USERNAME', 'u')
    monkeypatch.setattr(downsub.config, 'OPENSUBTITLES_PASSWORD', 'p')
    pc = downsub.build_provider_configs()
    assert pc['opensubtitlescom'] == {'username': 'u', 'password': 'p'}
