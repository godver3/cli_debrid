"""Plex Reported Issues: reports from Plex's "Report Issue" replace the reported file.

Runs against a temp database with the real schema (copied from the conftest schema
template), with the Plex calls faked. The scenario simulates successive task runs:
the first records the existing inbox, the next handles new reports, later ones
follow up once the replacements land, keep reporting the same movie, stay quiet,
and retry a failure.
"""
import json
import shutil
import sys
import types

import pytest

SERVER = '1326c35fea09ea0a4f5ff561c25bf023157b5ee9'
MOVIE_FILE = '/sym/Movies/The Conjuring 2 (2016)/conjuring2-1080p.mkv'

METADATA = {
    '100': {'type': 'movie', 'title': 'The Conjuring 2', 'year': 2016,
            'Guid': [{'id': 'imdb://tt3065204'}, {'id': 'tmdb://259693'}],
            'Media': [{'Part': [{'file': MOVIE_FILE}]}]},
    '200': {'type': 'episode', 'grandparentTitle': 'Friends', 'grandparentRatingKey': '201',
            'parentIndex': 10, 'index': 5, 'Guid': [{'id': 'tvdb://1'}],
            'Media': [{'Part': [{'file': '/plexview/Friends/S10E05.mkv'}]}]},
    '201': {'type': 'show', 'title': 'Friends', 'Guid': [{'id': 'imdb://tt0108778'}, {'id': 'tmdb://1668'}]},
    '300': {'type': 'season', 'parentTitle': 'Friends', 'title': 'Season 10'},
    '400': {'type': 'movie', 'title': 'Local Movie', 'year': 2001, 'Guid': [{'id': 'imdb://tt0000400'}],
            'Media': [{'Part': [{'file': '/my/own/copy.mkv'}]}]},
    '500': {'type': 'movie', 'title': 'Flaky', 'year': 2001, 'Guid': [{'id': 'imdb://tt0000400'}],
            'Media': [{'Part': [{'file': '/my/own/copy.mkv'}]}]},
}


def node(rid, key, message='broken', server=SERVER):
    return {'id': rid, 'message': message,
            'url': f'server://{server}/com.plexapp.plugins.library/library/metadata/{key}',
            'date': '2026-10-24T21:00:00Z',
            'user': {'id': 'u1', 'username': 'friend', 'displayName': 'Friend'}, 'commentCount': 0}


@pytest.fixture(scope='module')
def out(_schema_template, tmp_path_factory):
    """Run the whole scenario once and return what each step saw."""
    mp = pytest.MonkeyPatch()
    try:
        return _run_scenario(mp, _schema_template, tmp_path_factory.mktemp('reported_issues'))
    finally:
        mp.undo()


def _run_scenario(mp, template, tmp_path):
    shutil.copy(template, tmp_path / 'media_items.db')
    mp.setenv('USER_DB_CONTENT', str(tmp_path))
    from database.core import get_db_connection
    from database import plex_reported_issues as reports_db

    synced = []
    mp.setitem(sys.modules, 'routes.database_routes', types.SimpleNamespace(
        sync_queues_after_rescrape=lambda ids: synced.append(list(ids))))
    import utilities.plex_reported_issues as pri

    inbox, replies, notes, list_calls = [], [], [], []
    flaky = {'down': True}

    def fake_graphql(token, query, variables):
        if 'createReportComment' in query:
            replies.append((variables['input']['report'], variables['input']['message']))
            return {'createReportComment': {'id': 'c'}}
        list_calls.append(variables)
        return {'reports': {'nodes': list(inbox), 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}

    def metadata(url, token, key):
        if key == '500' and flaky['down']:
            raise ConnectionError('Plex unreachable')
        return METADATA.get(key)

    mp.setattr(pri, '_plex_credentials', lambda: ('http://plex:32400', 'TOKEN'))
    mp.setattr(pri, '_graphql', fake_graphql)
    mp.setattr(pri, '_server_machine_id', lambda u, t: SERVER)
    mp.setattr(pri, '_plex_metadata', metadata)
    mp.setattr(pri, '_notify_owner', notes.append)

    conn = get_db_connection()

    def add(**row):
        cur = conn.execute(f"INSERT INTO media_items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           list(row.values()))
        conn.commit()
        return cur.lastrowid

    ids = {
        'movie': add(imdb_id='tt3065204', tmdb_id='259693', title='The Conjuring 2', type='movie',
                     state='Collected', version='1080p*', filled_by_magnet='magnet:?xt=urn:btih:old',
                     filled_by_file='conjuring2-1080p.mkv', filled_by_torrent_id='T1', location_on_disk=MOVIE_FILE),
        'movie_4k': add(imdb_id='tt3065204', tmdb_id='259693', title='The Conjuring 2', type='movie',
                        state='Collected', version='2160p', filled_by_file='conjuring2-2160p.mkv',
                        filled_by_torrent_id='T2',
                        location_on_disk='/sym/Movies/The Conjuring 2 (2016)/conjuring2-2160p.mkv'),
        # Plex-mode path: Plex sees the file under a different mount, only the basename matches.
        'episode': add(imdb_id='tt0108778', tmdb_id='1668', title='Friends', type='episode', season_number=10,
                       episode_number=5, state='Collected', version='1080p',
                       filled_by_magnet='magnet:?xt=urn:btih:ep', filled_by_file='S10E05.mkv',
                       filled_by_torrent_id='T3', location_on_disk='/mnt/zurg/Friends/S10E05.mkv'),
        'other_episode': add(imdb_id='tt0108778', tmdb_id='1668', title='Friends', type='episode',
                             season_number=10, episode_number=6, state='Collected', version='1080p',
                             filled_by_file='S10E06.mkv', location_on_disk='/mnt/zurg/Friends/S10E06.mkv'),
        'not_ours': add(imdb_id='tt0000400', title='Local Movie', type='movie', state='Collected',
                        version='1080p', filled_by_file='release.mkv', location_on_disk='/sym/Local Movie/release.mkv'),
    }

    def rows():
        return {r['id']: dict(r) for r in conn.execute("SELECT * FROM media_items").fetchall()}

    def statuses():
        return {r['report_id']: r['status'] for r in conn.execute("SELECT * FROM plex_reported_issues").fetchall()}

    def land_replacement():
        conn.execute("UPDATE media_items SET state = 'Collected', location_on_disk = ?, "
                     "filled_by_file = 'conjuring2-1080p.mkv', replaced_files = NULL WHERE id = ?",
                     (MOVIE_FILE, ids['movie']))
        conn.commit()

    out = {'ids': ids, 'synced': synced}
    # Run 1: the feature was just turned on and one old report is waiting.
    inbox.append(node('old', '100'))
    out['run1'] = pri.process_reported_issues()
    out['run1_rows'] = rows()
    out['run1_replies'] = list(replies)

    # Run 2: new reports (Plex lists newest first).
    inbox[:0] = [node('dupe', '100'), node('season', '300'), node('other_server', '100', server='ffff'),
                 node('local', '400'), node('gone', '999'), node('episode', '200'), node('movie', '100')]
    out['run2'] = pri.process_reported_issues()
    out['run2_rows'] = rows()
    out['run2_statuses'] = statuses()
    out['run2_replies'] = list(replies)
    out['run2_notes'] = list(notes)

    # Run 3: the movie's replacement landed; the episode found nothing else.
    land_replacement()
    conn.execute("UPDATE media_items SET state = 'Blacklisted' WHERE id = ?", (ids['episode'],))
    conn.commit()
    replies.clear()
    notes.clear()
    out['run3'] = pri.process_reported_issues()
    out['run3_statuses'] = statuses()
    out['run3_replies'] = list(replies)
    out['run3_notes'] = list(notes)

    # Runs 4-6: the movie keeps getting reported after each replacement lands.
    out['cap'] = []
    for rid in ('again1', 'again2', 'again3'):
        inbox.insert(0, node(rid, '100'))
        replies.clear()
        out['cap'].append(pri.process_reported_issues())
        land_replacement()
    out['cap_statuses'] = statuses()
    out['cap_replies'] = list(replies)

    # Quiet check: nothing new is one page request, and nothing is handled.
    list_calls.clear()
    out['quiet'] = pri.process_reported_issues()
    out['quiet_calls'] = len(list_calls)

    # A report that errors is saved as failed, left alone for a few minutes, then retried.
    inbox.insert(0, node('flaky', '500'))
    out['flaky1'] = pri.process_reported_issues()
    out['flaky2'] = pri.process_reported_issues()  # too soon to retry
    flaky['down'] = False
    conn.execute("UPDATE plex_reported_issues SET handled_at = '2000-01-01 00:00:00' WHERE report_id = 'flaky'")
    conn.commit()
    out['flaky3'] = pri.process_reported_issues()
    out['flaky_status'] = statuses()['flaky']

    out['listed'] = [r['report_id'] for r in reports_db.get_reports()]
    conn.close()
    return out


def row(out, run, name):
    return out[f'{run}_rows'][out['ids'][name]]


def test_first_run_only_records_the_existing_inbox(out):
    assert out['run1'] == {'baseline': 1}
    assert row(out, 'run1', 'movie')['state'] == 'Collected'
    assert out['run1_replies'] == []


def test_reported_movie_goes_back_to_wanted_without_its_release(out):
    movie = row(out, 'run2', 'movie')
    assert movie['state'] == 'Wanted'
    assert movie['excluded_release_id'] == 'magnet:?xt=urn:btih:old'
    assert movie['version'] == '1080p'
    assert json.loads(movie['replaced_files'])[0]['location_on_disk'] == MOVIE_FILE
    assert [out['ids']['movie']] in out['synced']


def test_other_version_plex_did_not_play_is_untouched(out):
    assert row(out, 'run2', 'movie_4k')['state'] == 'Collected'


def test_episode_matched_by_show_ids_and_basename(out):
    assert row(out, 'run2', 'episode')['state'] == 'Wanted'
    assert row(out, 'run2', 'other_episode')['state'] == 'Collected'


def test_statuses_after_handling(out):
    assert out['run2_statuses'] == {
        '__initialized__': 'baseline', 'old': 'baseline', 'movie': 'replacing', 'episode': 'replacing',
        'dupe': 'replacing', 'season': 'needs_review', 'other_server': 'ignored', 'local': 'no_match',
        'gone': 'ignored'}
    assert out['run2'] == {'replacing': 3, 'needs_review': 1, 'ignored': 2, 'no_match': 1}


def test_file_cli_debrid_does_not_manage_is_not_replaced(out):
    assert row(out, 'run2', 'not_ours')['state'] == 'Collected'
    assert any("doesn't manage" in n for n in out['run2_notes'])


def test_reporters_get_replies(out):
    replies = dict(out['run2_replies'])
    assert 'Finding a different copy of The Conjuring 2 (2016)' in replies['movie']
    assert 'Friends S10E05' in replies['episode']
    assert 'already being replaced' in replies['dupe']
    assert 'specific episode' in replies['season']
    assert 'other_server' not in replies
    assert 'local' not in replies


def test_follow_up_once_replaced_or_stuck(out):
    statuses = out['run3_statuses']
    assert statuses['movie'] == 'replaced'
    assert statuses['dupe'] == 'replaced'
    assert statuses['episode'] == 'needs_review'
    done = dict(out['run3_replies'])
    assert 'has been replaced' in done['movie']
    assert 'episode' not in done
    assert any("Couldn't find a different copy of Friends S10E05" in n for n in out['run3_notes'])
    assert out['run3'] == {}


def test_repeat_reports_stop_after_three_replacements_a_day(out):
    assert out['cap'] == [{'replacing': 1}, {'replacing': 1}, {'needs_review': 1}]
    assert out['cap_statuses']['again3'] == 'needs_review'
    assert 'server owner has been asked' in dict(out['cap_replies'])['again3']


def test_quiet_check_is_one_request(out):
    assert out['quiet'] == {}
    assert out['quiet_calls'] == 1


def test_failed_report_is_retried_later(out):
    assert out['flaky1'] == {'failed': 1}
    assert out['flaky2'] == {}
    assert out['flaky3'] == {'no_match': 1}
    assert out['flaky_status'] == 'no_match'


def test_listing_hides_baseline_rows(out):
    assert 'old' not in out['listed']
    assert '__initialized__' not in out['listed']
    assert 'movie' in out['listed']
