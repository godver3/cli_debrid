"""Plex Reported Issues: reports from Plex's "Report Issue" replace the reported file.

Runs in a fresh interpreter against a temp database with the real schema (other
tests leave stubs for database modules in sys.modules). Plex HTTP calls are
replaced with fakes; three task runs are simulated: the first records the
existing inbox, the second handles new reports, the third follows up once the
replacements land.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent('''
    import json, sys, types
    sys.path.insert(0, %(root)r)
    import database  # noqa
    from database.schema_management import verify_database
    verify_database()
    from database.core import get_db_connection
    from database import plex_reported_issues as reports_db

    synced = []
    sys.modules['routes.database_routes'] = types.SimpleNamespace(
        sync_queues_after_rescrape=lambda ids: synced.append(list(ids)))

    import utilities.plex_reported_issues as pri

    SERVER = '1326c35fea09ea0a4f5ff561c25bf023157b5ee9'
    def url(key, server=SERVER):
        return f'server://{server}/com.plexapp.plugins.library/library/metadata/{key}'
    def node(rid, key, message='broken', server=SERVER):
        return {'id': rid, 'message': message, 'url': url(key, server), 'date': '2026-10-24T21:00:00Z',
                'user': {'id': 'u1', 'username': 'friend', 'displayName': 'Friend'}, 'commentCount': 0}

    METADATA = {
        '100': {'type': 'movie', 'title': 'The Conjuring 2', 'year': 2016,
                'Guid': [{'id': 'imdb://tt3065204'}, {'id': 'tmdb://259693'}],
                'Media': [{'Part': [{'file': '/sym/Movies/The Conjuring 2 (2016)/conjuring2-1080p.mkv'}]}]},
        '200': {'type': 'episode', 'grandparentTitle': 'Friends', 'grandparentRatingKey': '201',
                'parentIndex': 10, 'index': 5, 'Guid': [{'id': 'tvdb://1'}],
                'Media': [{'Part': [{'file': '/plexview/Friends/S10E05.mkv'}]}]},
        '201': {'type': 'show', 'title': 'Friends', 'Guid': [{'id': 'imdb://tt0108778'}, {'id': 'tmdb://1668'}]},
        '300': {'type': 'season', 'parentTitle': 'Friends', 'title': 'Season 10'},
        '400': {'type': 'movie', 'title': 'Local Movie', 'year': 2001, 'Guid': [{'id': 'imdb://tt0000400'}],
                'Media': [{'Part': [{'file': '/my/own/copy.mkv'}]}]},
    }
    inbox, replies, notes = [], [], []

    list_calls = []
    def fake_graphql(token, query, variables):
        if 'createReportComment' not in query:
            list_calls.append(variables)
        if 'createReportComment' in query:
            replies.append((variables['input']['report'], variables['input']['message']))
            return {'createReportComment': {'id': 'c'}}
        return {'reports': {'nodes': list(inbox), 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}

    pri._plex_credentials = lambda: ('http://plex:32400', 'TOKEN')
    pri._graphql = fake_graphql
    pri._server_machine_id = lambda u, t: SERVER
    pri._plex_metadata = lambda u, t, key: METADATA.get(key)
    pri._notify_owner = notes.append

    conn = get_db_connection()
    def add(**row):
        cur = conn.execute(f"INSERT INTO media_items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           list(row.values()))
        conn.commit()
        return cur.lastrowid
    movie = add(imdb_id='tt3065204', tmdb_id='259693', title='The Conjuring 2', type='movie', state='Collected',
                version='1080p*', filled_by_magnet='magnet:?xt=urn:btih:old', filled_by_file='conjuring2-1080p.mkv',
                filled_by_torrent_id='T1', location_on_disk='/sym/Movies/The Conjuring 2 (2016)/conjuring2-1080p.mkv')
    movie_4k = add(imdb_id='tt3065204', tmdb_id='259693', title='The Conjuring 2', type='movie', state='Collected',
                   version='2160p', filled_by_file='conjuring2-2160p.mkv', filled_by_torrent_id='T2',
                   location_on_disk='/sym/Movies/The Conjuring 2 (2016)/conjuring2-2160p.mkv')
    # Plex-mode path: Plex sees the file under a different mount, only the basename matches.
    episode = add(imdb_id='tt0108778', tmdb_id='1668', title='Friends', type='episode', season_number=10,
                  episode_number=5, state='Collected', version='1080p', filled_by_magnet='magnet:?xt=urn:btih:ep',
                  filled_by_file='S10E05.mkv', filled_by_torrent_id='T3', location_on_disk='/mnt/zurg/Friends/S10E05.mkv')
    other_episode = add(imdb_id='tt0108778', tmdb_id='1668', title='Friends', type='episode', season_number=10,
                        episode_number=6, state='Collected', version='1080p', filled_by_file='S10E06.mkv',
                        location_on_disk='/mnt/zurg/Friends/S10E06.mkv')
    not_ours = add(imdb_id='tt0000400', title='Local Movie', type='movie', state='Collected', version='1080p',
                   filled_by_file='release.mkv', location_on_disk='/sym/Local Movie/release.mkv')

    def rows():
        return {r['id']: dict(r) for r in conn.execute("SELECT * FROM media_items").fetchall()}
    def statuses():
        return {r['report_id']: r['status'] for r in conn.execute("SELECT * FROM plex_reported_issues").fetchall()}

    out = {}
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
    out['synced'] = synced

    # Run 3: the movie's replacement landed; the episode found nothing else.
    MOVIE_FILE = METADATA['100']['Media'][0]['Part'][0]['file']
    def land_replacement():
        conn.execute("UPDATE media_items SET state = 'Collected', location_on_disk = ?, "
                     "filled_by_file = 'conjuring2-1080p.mkv', replaced_files = NULL WHERE id = ?",
                     (MOVIE_FILE, movie))
    land_replacement()
    conn.execute("UPDATE media_items SET state = 'Blacklisted' WHERE id = ?", (episode,))
    conn.commit()
    replies.clear(); notes.clear()
    out['run3'] = pri.process_reported_issues()
    out['run3_statuses'] = statuses()
    out['run3_replies'] = list(replies)
    out['run3_notes'] = list(notes)
    # Runs 4-6: the movie keeps getting reported after each replacement lands.
    cap = []
    for rid in ('again1', 'again2', 'again3'):
        inbox.insert(0, node(rid, '100'))
        replies.clear(); notes.clear()
        cap.append(pri.process_reported_issues())
        land_replacement()
        conn.commit()
    out['cap'] = cap
    out['cap_statuses'] = statuses()
    out['cap_replies'] = list(replies)
    # Quiet check: nothing new is one page request, and nothing is handled.
    list_calls.clear()
    out['quiet'] = pri.process_reported_issues()
    out['quiet_calls'] = len(list_calls)

    # A report that errors is saved as failed, left alone for a few minutes, then retried.
    METADATA['500'] = dict(METADATA['400'], title='Flaky')
    real_metadata = pri._plex_metadata
    def flaky(u, t, key):
        if key == '500':
            raise ConnectionError('Plex unreachable')
        return real_metadata(u, t, key)
    pri._plex_metadata = flaky
    inbox.insert(0, node('flaky', '500'))
    out['flaky1'] = pri.process_reported_issues()
    out['flaky2'] = pri.process_reported_issues()  # too soon to retry
    pri._plex_metadata = real_metadata
    conn.execute("UPDATE plex_reported_issues SET handled_at = '2000-01-01 00:00:00' WHERE report_id = 'flaky'")
    conn.commit()
    out['flaky3'] = pri.process_reported_issues()
    out['flaky_status'] = statuses()['flaky']
    out['listed'] = [r['report_id'] for r in reports_db.get_reports()]
    out['ids'] = dict(movie=movie, movie_4k=movie_4k, episode=episode, other_episode=other_episode, not_ours=not_ours)
    print('RESULT ' + json.dumps(out, default=str))
''')


class PlexReportedIssuesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, USER_DB_CONTENT=tmp, USER_CONFIG=tmp, USER_LOGS=tmp, PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-c', SCRIPT % {'root': ROOT}], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=300)
        lines = [l for l in proc.stdout.splitlines() if l.startswith('RESULT ')]
        if not lines:
            raise AssertionError(f"probe failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        cls.out = json.loads(lines[-1][len('RESULT '):])
        cls.ids = cls.out['ids']

    def row(self, run, name):
        return self.out[f'{run}_rows'][str(self.ids[name])]

    def test_first_run_only_records_the_existing_inbox(self):
        self.assertEqual(self.out['run1'], {'baseline': 1})
        self.assertEqual(self.row('run1', 'movie')['state'], 'Collected')
        self.assertEqual(self.out['run1_replies'], [])

    def test_reported_movie_goes_back_to_wanted_without_its_release(self):
        movie = self.row('run2', 'movie')
        self.assertEqual(movie['state'], 'Wanted')
        self.assertEqual(movie['excluded_release_id'], 'magnet:?xt=urn:btih:old')
        self.assertEqual(movie['version'], '1080p')
        stashed = json.loads(movie['replaced_files'])
        self.assertEqual(stashed[0]['location_on_disk'], '/sym/Movies/The Conjuring 2 (2016)/conjuring2-1080p.mkv')
        self.assertIn([self.ids['movie']], self.out['synced'])

    def test_other_version_plex_did_not_play_is_untouched(self):
        self.assertEqual(self.row('run2', 'movie_4k')['state'], 'Collected')

    def test_episode_matched_by_show_ids_and_basename(self):
        self.assertEqual(self.row('run2', 'episode')['state'], 'Wanted')
        self.assertEqual(self.row('run2', 'other_episode')['state'], 'Collected')

    def test_statuses_after_handling(self):
        self.assertEqual(self.out['run2_statuses'], {
            '__initialized__': 'baseline', 'old': 'baseline', 'movie': 'replacing', 'episode': 'replacing',
            'dupe': 'replacing', 'season': 'needs_review', 'other_server': 'ignored', 'local': 'no_match',
            'gone': 'ignored'})
        self.assertEqual(self.out['run2'], {'replacing': 3, 'needs_review': 1, 'ignored': 2, 'no_match': 1})

    def test_file_cli_debrid_does_not_manage_is_not_replaced(self):
        self.assertEqual(self.row('run2', 'not_ours')['state'], 'Collected')
        self.assertTrue(any("doesn't manage" in n for n in self.out['run2_notes']))

    def test_reporters_get_replies(self):
        replies = dict(self.out['run2_replies'])
        self.assertIn('Finding a different copy of The Conjuring 2 (2016)', replies['movie'])
        self.assertIn('Friends S10E05', replies['episode'])
        self.assertIn('already being replaced', replies['dupe'])
        self.assertIn('specific episode', replies['season'])
        self.assertNotIn('other_server', replies)
        self.assertNotIn('local', replies)

    def test_follow_up_once_replaced_or_stuck(self):
        statuses = self.out['run3_statuses']
        self.assertEqual(statuses['movie'], 'replaced')
        self.assertEqual(statuses['dupe'], 'replaced')
        self.assertEqual(statuses['episode'], 'needs_review')
        done = dict(self.out['run3_replies'])
        self.assertIn('has been replaced', done['movie'])
        self.assertNotIn('episode', done)
        self.assertTrue(any("Couldn't find a different copy of Friends S10E05" in n for n in self.out['run3_notes']))
        self.assertEqual(self.out['run3'], {})

    def test_repeat_reports_stop_after_three_replacements_a_day(self):
        self.assertEqual(self.out['cap'][0], {'replacing': 1})
        self.assertEqual(self.out['cap'][1], {'replacing': 1})
        self.assertEqual(self.out['cap'][2], {'needs_review': 1})
        self.assertEqual(self.out['cap_statuses']['again3'], 'needs_review')
        self.assertIn('server owner has been asked', dict(self.out['cap_replies'])['again3'])

    def test_quiet_check_is_one_request(self):
        self.assertEqual(self.out['quiet'], {})
        self.assertEqual(self.out['quiet_calls'], 1)

    def test_failed_report_is_retried_later(self):
        self.assertEqual(self.out['flaky1'], {'failed': 1})
        self.assertEqual(self.out['flaky2'], {})
        self.assertEqual(self.out['flaky3'], {'no_match': 1})
        self.assertEqual(self.out['flaky_status'], 'no_match')

    def test_listing_hides_baseline_rows(self):
        self.assertNotIn('old', self.out['listed'])
        self.assertNotIn('__initialized__', self.out['listed'])
        self.assertIn('movie', self.out['listed'])


if __name__ == '__main__':
    unittest.main()
