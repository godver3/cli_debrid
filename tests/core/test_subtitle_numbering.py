"""Subtitles for shows numbered differently on the subtitle sites (Dragon Ball:
TVDB S05E31 in the database, S01E132 in the file name and on TMDB, and
OpenSubtitles' own season split): never another episode's subtitle, and the
same episode found under a proven other numbering."""
from types import SimpleNamespace
from unittest import mock

import pytest

pytest.importorskip('subliminal')


def _episode(season, episode, name='Dragon Ball S01E132.mkv'):
    from subliminal.video import Episode
    return Episode(name, 'Dragon Ball', season, episode)


def _sub(release, provider='opensubtitlescom', matches=('series', 'season', 'episode')):
    return SimpleNamespace(release_name=release, provider_name=provider, get_matches=lambda v: set(matches))


def test_release_episodes():
    from utilities.downsub import release_episodes
    assert release_episodes('Dragon Ball - S06E21 - The End, The Beginning') == {(6, 21)}
    assert release_episodes('Abbott.Elementary.S03E01-E02.1080p.WEB.H264-ETHEL') == {(3, 1), (3, 2)}
    assert release_episodes('Dragon.Ball.s01e153.1080p') == {(1, 153)}
    assert release_episodes('Show.S03.1080p.WEB.x264') == set()          # season pack: says nothing
    assert release_episodes('Movie.2020.1080p.x265-GRP') == set()


def test_other_episode_release_is_rejected():
    from utilities.downsub import names_other_episode
    allowed = {(5, 31), (1, 132)}                     # database + file numbering
    assert names_other_episode(_sub('Dragon Ball - S06E21 - The End, The Beginning'), allowed)
    assert not names_other_episode(_sub('Dragon Ball - S01E132 - Hotter Than Lava'), allowed)
    assert not names_other_episode(_sub('Dragon Ball S05E31'), allowed)
    assert not names_other_episode(_sub('Dragon.Ball.Complete.Series'), allowed)   # no S/E: can't tell


def test_ranker_scores_other_episode_zero():
    from utilities.downsub import _ranker
    with mock.patch('subliminal.score.compute_score', return_value=900):
        rank = _ranker('Dragon Ball S01E132', {(5, 31), (1, 132)})
        assert rank(_sub('Dragon Ball - S06E21 - The End'), _episode(5, 31)) == 0
        assert rank(_sub('Dragon Ball - S01E132 - Hotter Than Lava'), _episode(5, 31)) > 900 * 10000


def test_file_numbering():
    from utilities.downsub import _file_numbering
    assert _file_numbering({'item': {'filled_by_file': 'Dragon Ball S01E153.mkv'}}) == (1, 153)
    assert _file_numbering({'item': {'filled_by_file': 'Some.Movie.2020.mkv'}}) is None


def test_tmdb_numbering_needs_same_day_or_title():
    from utilities import downsub
    eps = [(1, 152, '1989-04-12', 'Mystery of the Dark World'), (1, 153, '1989-04-19', 'The End, the Beginning'),
           (2, 1, '1989-04-26', 'Another Show Episode')]
    item = {'tmdb_id': '12609', 'release_date': '1989-04-19', 'episode_title': 'The End, The Beginning'}
    with mock.patch.object(downsub, '_tmdb_show_episodes', return_value=eps):
        assert downsub._tmdb_numbering(item) == (1, 153)
        # a day off, same title: still the episode
        assert downsub._tmdb_numbering(dict(item, release_date='1989-04-18')) == (1, 153)
        # same day but a clearly different title: not proof
        assert downsub._tmdb_numbering(dict(item, episode_title='Totally Different Name')) is None
        assert downsub._tmdb_numbering(dict(item, release_date='1990-01-01')) is None


def test_search_retries_under_proven_numbering():
    """Nothing as S05E31: retried as the file's S01E132, found, and saved
    under that numbering; the wrong-episode guard allows it."""
    from utilities import downsub
    video = _episode(5, 31)
    ctx = {'video': video, 'languages': ['en'], 'min_score': 594, 'parse_name': 'Dragon Ball S01E132',
           'item': {'id': 7, 'title': 'Dragon Ball', 'type': 'episode', 'season_number': 5, 'episode_number': 31,
                    'filled_by_file': 'Dragon Ball S01E132.mkv'},
           'original_path': '/lib/Dragon Ball S01E132.mkv', 'allowed_se': {(5, 31), (1, 132)},
           'all_embedded': False}
    found = _sub('Dragon Ball - S01E132 - Hotter Than Lava')
    found.language = SimpleNamespace(ietf='en')
    asked = []

    def best(videos, languages, **kw):
        v = next(iter(videos))
        asked.append((v.season, v.episodes[0]))
        return {v: [found] if (v.season, v.episodes[0]) == (1, 132) else []}
    with mock.patch.object(downsub, 'prepare_video', return_value=ctx), \
            mock.patch.object(downsub, 'download_best_subtitles', side_effect=best), \
            mock.patch.object(downsub, 'selected_providers', return_value=['opensubtitlescom']), \
            mock.patch.object(downsub, '_tmdb_numbering', return_value=(1, 132)), \
            mock.patch.object(downsub, '_score_text', return_value='score'), \
            mock.patch.object(downsub, 'save_subtitles', return_value=1) as save, \
            mock.patch.object(downsub, '_record_attempt'):
        assert downsub.download_subtitles_for_video('/lib/Dragon Ball S01E132.mkv', item=ctx['item'])
    assert asked == [(5, 31), (1, 132)]          # TMDB gives the same pair as the file: asked once
    assert save.called and (video.season, video.episodes) == (1, [132])


def test_score_alone_is_not_enough_for_an_episode():
    """Series + year + country (810) beat the 594 floor with no season or
    episode match: SubSource's 'Curse of the Blood Rubies' for S01E132."""
    from utilities.downsub import _ranker, identity_ok
    ep = _episode(1, 132)
    loose = _sub('Dragon Ball Curse of the Blood Rubies', 'subsource', ('series', 'year', 'country'))
    assert not identity_ok(loose, ep)
    assert identity_ok(_sub('x', matches=('hash',)), ep)
    with mock.patch('subliminal.score.compute_score', return_value=810):
        assert _ranker('Dragon Ball S01E132', {(1, 132)})(loose, ep) == 0
