"""The simulated Plex answers the calls cli_debrid makes, in the shapes plexapi and
the app's JSON parsing expect. If this breaks after a plexapi upgrade, the bench
scenarios are no longer exercising the real client paths."""
import json
import os
import urllib.request

import pytest
from plexapi.server import PlexServer

from tests.plex_bench.clock import SimClock
from tests.plex_bench.fake_plex import Fault, FakePlex, Meta

MOVIE = 'Dune.2021.1080p-GRP.mkv'


@pytest.fixture
def plex(tmp_path):
    for d in ('movies/Dune.2021.1080p-GRP', 'tv/Show.S01.1080p-GRP'):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / 'movies/Dune.2021.1080p-GRP' / MOVIE).write_bytes(b'x')
    for e in (1, 2):
        (tmp_path / f'tv/Show.S01.1080p-GRP/Show.S01E0{e}.1080p-GRP.mkv').write_bytes(b'x')
    fake = FakePlex(SimClock(), sections=[
        dict(key=1, title='Movies', type='movie', locations=[str(tmp_path / 'movies')]),
        dict(key=2, title='Shows', type='show', locations=[str(tmp_path / 'tv')]),
    ]).start()
    fake.register(MOVIE, Meta('movie', 'Dune', 2021, 'tt1160419', '438631'))
    for e in (1, 2):
        fake.register(f'Show.S01E0{e}.1080p-GRP.mkv', Meta('episode', 'Show', 2020, 'tt9999', None, '777', 1, e))
    fake.root = str(tmp_path)
    yield fake
    fake.stop()


def _scan_everything(fake, server):
    for section in server.library.sections():
        section.update(path=section.locations[0])
    fake.clock.advance(3600)
    fake.advance(fake.clock.time())


def test_scans_queue_and_run_one_at_a_time(plex):
    server = PlexServer(plex.url, plex.token)
    movies, shows = server.library.sections()
    movies.update(path=os.path.join(plex.root, 'movies/Dune.2021.1080p-GRP'))
    shows.update(path=os.path.join(plex.root, 'tv/Show.S01.1080p-GRP'))
    assert plex.scan_backlog() == 2

    plex.clock.advance(5)
    plex.advance(plex.clock.time())
    assert plex.indexed_paths() == set()  # 5 s overhead + 1 s per file

    plex.clock.advance(60)
    plex.advance(plex.clock.time())
    done = [s['done_at'] - s['requested_at'] for s in plex.scans_completed]
    assert done == [6, 6 + 7]  # the second waited for the first
    assert len(plex.indexed_paths()) == 3


def test_identical_queued_scans_are_merged_running_ones_are_not(plex):
    server = PlexServer(plex.url, plex.token)
    movies = server.library.sections()[0]
    folder = os.path.join(plex.root, 'movies/Dune.2021.1080p-GRP')
    movies.update(path=folder)           # starts at once
    movies.update(path=folder)           # queued behind it
    movies.update(path=folder)           # same as the queued one: merged
    plex.advance(plex.clock.time())
    assert [e['accepted'] for e in plex.scans_requested] == [True, True, False]


def test_lookups_cli_debrid_uses(plex):
    server = PlexServer(plex.url, plex.token)
    _scan_everything(plex, server)
    movies, shows = server.library.sections()

    found = server.fetchItems(f'/library/sections/1/all?file={MOVIE}')
    assert found[0].media[0].parts[0].file.endswith(MOVIE)
    episode = server.fetchItems('/library/sections/2/all?file=Show.S01E02.1080p-GRP.mkv&type=4')[0]
    assert (episode.parentIndex, episode.index) == (1, 2)
    # Like a real Plex with the new agents: only the plex:// guid matches.
    dune = found[0]
    assert movies.search(guid='imdb://tt1160419') == []
    assert movies.search(guid=dune.guid)[0].ratingKey == dune.ratingKey
    show = server.fetchItem(episode.grandparentRatingKey)
    assert show.episode(season=1, episode=2).ratingKey == episode.ratingKey
    with pytest.raises(Exception):  # a show library needs type=4 for ?file=
        server.fetchItems('/library/sections/2/all?file=Show.S01E02.1080p-GRP.mkv')


def test_recently_added_json_lists_episodes(plex):
    server = PlexServer(plex.url, plex.token)
    _scan_everything(plex, server)
    req = urllib.request.Request(plex.url + '/library/sections/2/recentlyAdded?X-Plex-Container-Size=200',
                                 headers={'X-Plex-Token': plex.token, 'Accept': 'application/json'})
    data = json.load(urllib.request.urlopen(req))['MediaContainer']['Metadata']
    assert [(m['type'], m['index'], isinstance(m['addedAt'], int)) for m in data] == [('episode', 2, True), ('episode', 1, True)]


def test_wrong_token_and_faults(plex):
    with pytest.raises(Exception):
        PlexServer(plex.url, 'wrong')
    plex.behavior.faults.append(Fault('.*', 'drop', times=1))
    with pytest.raises(Exception):
        PlexServer(plex.url, plex.token)
    PlexServer(plex.url, plex.token)  # the fault was used up
    assert [r.get('fault') for r in plex.requests[-2:]] == ['drop', None]
