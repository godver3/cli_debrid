"""NFO files for the symlinked TV library (utilities/nfo_writer.py)."""
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from utilities import nfo_writer as nw

SEASONS = {
    1: {'episodes': {
        1: {'title': 'Red Bones (1)', 'overview': 'Mima is found & dead <here>.', 'first_aired': '2013-03-10T21:00:00',
            'runtime': 57, 'tvdb_id': 4369401},
        2: {'title': 'Red Bones (2)', 'overview': 'Part two.', 'first_aired': '2013-03-11T21:00:00',
            'runtime': 58, 'tvdb_id': 4369402},
        3: {'title': 'Raven', 'overview': '', 'first_aired': '2013-03-12', 'tvdb_id': 3}}},
}
META = {'title': 'Shetland', 'year': 2013, 'overview': 'Crime on the islands.', 'runtime': 59,
        'status': 'returning series', 'network': 'BBC One', 'genres': ['Drama', 'Crime'],
        'ids': {'imdb': 'tt2396135', 'tvdb': 261109, 'tmdb': 46923}}


def _parse_episodes(text):
    body = text.split('-->', 1)[1]
    return ET.fromstring(f'<root>{body}</root>').findall('episodedetails')


class NfoWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, 'symlinks')
        self.show = os.path.join(self.root, 'TV Shows', 'Shetland (2013)')
        self.s1 = os.path.join(self.show, 'Season 01')
        os.makedirs(self.s1)
        self.rows = []
        for e in (1, 2):
            path = os.path.join(self.s1, f'Shetland (2013) - S01E0{e} - x.mkv')
            os.symlink('/nonexistent/target.mkv', path)
            self.rows.append({'id': e, 'imdb_id': 'tt2396135', 'tmdb_id': '46923', 'title': 'Shetland', 'year': 2013,
                              'season_number': 1, 'episode_number': e, 'episode_title': f'db title {e}',
                              'release_date': '2013-03-10', 'runtime': 60, 'location_on_disk': path})
        patches = [
            mock.patch.object(nw, 'library_root', return_value=self.root),
            mock.patch.object(nw, '_battery', return_value=(META, SEASONS)),
            mock.patch.object(nw, '_show_imdb_ids_in', return_value={'tt2396135'}),
            mock.patch.object(nw, '_episode_rows', side_effect=lambda imdb_id=None: [dict(r) for r in self.rows]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_writes_show_and_episode_nfos_from_battery_data(self):
        c = nw.write_show('tt2396135')
        self.assertEqual(c['written'], 3)
        show = ET.fromstring(self._read(os.path.join(self.show, 'tvshow.nfo')).split('-->', 1)[1])
        self.assertEqual(show.tag, 'tvshow')
        self.assertEqual(show.findtext('title'), 'Shetland')
        self.assertEqual(show.findtext('premiered'), '2013-03-10')
        self.assertEqual(show.findtext('status'), 'Continuing')
        self.assertEqual([g.text for g in show.findall('genre')], ['Drama', 'Crime'])
        ids = {u.get('type'): (u.text, u.get('default')) for u in show.findall('uniqueid')}
        self.assertEqual(ids['tvdb'], ('261109', 'true'))
        self.assertEqual(ids['imdb'], ('tt2396135', None))
        ep = _parse_episodes(self._read(self.rows[0]['location_on_disk'][:-4] + '.nfo'))
        self.assertEqual(len(ep), 1)
        self.assertEqual(ep[0].findtext('title'), 'Red Bones (1)')   # battery title, not the DB copy
        self.assertEqual(ep[0].findtext('plot'), 'Mima is found & dead <here>.')  # escaped and parsed back
        self.assertEqual(ep[0].findtext('aired'), '2013-03-10')
        self.assertEqual((ep[0].findtext('season'), ep[0].findtext('episode')), ('1', '1'))

    def test_unchanged_files_are_not_rewritten(self):
        nw.write_show('tt2396135')
        nfo = self.rows[0]['location_on_disk'][:-4] + '.nfo'
        before = os.stat(nfo).st_mtime_ns
        c = nw.write_show('tt2396135')
        self.assertEqual((c['written'], c['unchanged']), (0, 3))
        self.assertEqual(os.stat(nfo).st_mtime_ns, before)

    def test_foreign_nfo_is_left_alone(self):
        nfo = self.rows[0]['location_on_disk'][:-4] + '.nfo'
        with open(nfo, 'w') as f:
            f.write('<episodedetails><title>mine</title></episodedetails>')
        c = nw.write_show('tt2396135')
        self.assertEqual(c['foreign'], 1)
        self.assertIn('mine', self._read(nfo))
        nw.remove_for_video(self.rows[0]['location_on_disk'])
        self.assertTrue(os.path.exists(nfo))

    def test_multi_episode_file_gets_one_block_per_episode(self):
        self.rows[1]['location_on_disk'] = self.rows[0]['location_on_disk']
        nw.write_show('tt2396135')
        eps = _parse_episodes(self._read(self.rows[0]['location_on_disk'][:-4] + '.nfo'))
        self.assertEqual([e.findtext('episode') for e in eps], ['1', '2'])

    def test_episode_falls_back_to_db_fields(self):
        self.rows[0]['episode_number'] = 9
        nw.write_show('tt2396135')
        ep = _parse_episodes(self._read(self.rows[0]['location_on_disk'][:-4] + '.nfo'))[0]
        self.assertEqual(ep.findtext('title'), 'db title 1')
        self.assertEqual(ep.findtext('episode'), '9')

    def test_shared_folder_gets_no_tvshow_nfo(self):
        with mock.patch.object(nw, '_show_imdb_ids_in', return_value={'tt2396135', 'tt0000001'}):
            c = nw.write_show('tt2396135')
        self.assertEqual(c['skipped_folders'], 1)
        self.assertFalse(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))

    def test_flat_layout_never_writes_into_library_root(self):
        self.assertIsNone(nw.show_folder(os.path.join(self.root, 'x.mkv'), self.root))
        self.assertIsNone(nw.show_folder(os.path.join(self.root, 'Season 01', 'x.mkv'), self.root))
        self.assertEqual(nw.show_folder(self.rows[0]['location_on_disk'], self.root), self.show)

    def test_remove_for_video_drops_episode_then_show_nfo(self):
        nw.write_show('tt2396135')
        first, second = (r['location_on_disk'] for r in self.rows)
        os.unlink(first)
        nw.remove_for_video(first)
        self.assertFalse(os.path.exists(first[:-4] + '.nfo'))
        self.assertTrue(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))  # S01E02 still there
        os.unlink(second)
        nw.remove_for_video(second)
        self.assertFalse(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))
        self.assertEqual(os.listdir(self.s1), [])  # folder can empty

    def test_renamed_file_leaves_no_orphan_nfo(self):
        nw.write_show('tt2396135')
        old = self.rows[0]['location_on_disk']
        new = old.replace(' - x.mkv', ' - y.mkv')
        os.rename(old, new)
        self.rows[0]['location_on_disk'] = new
        c = nw.write_show('tt2396135')
        self.assertEqual(c['removed'], 1)
        self.assertFalse(os.path.exists(old[:-4] + '.nfo'))
        self.assertTrue(os.path.exists(new[:-4] + '.nfo'))

    def test_remove_library_deletes_only_ours(self):
        nw.write_show('tt2396135')
        mine = os.path.join(self.s1, 'user.nfo')
        with open(mine, 'w') as f:
            f.write('<tvshow/>')
        self.assertEqual(nw.remove_library()['removed'], 3)
        self.assertTrue(os.path.exists(mine))

    def test_windows_line_ends_do_not_cause_rewrites(self):
        with mock.patch.object(nw, '_battery', return_value=(dict(META, overview='a\r\nb'), SEASONS)):
            nw.write_show('tt2396135')
            c = nw.write_show('tt2396135')
        self.assertEqual(c['written'], 0)

    def test_control_characters_are_dropped(self):
        xml = nw.build_episode_nfo([{'season': 1, 'episode': 1, 'title': 'a\x01b'}], 'S')
        self.assertEqual(_parse_episodes(xml)[0].findtext('title'), 'ab')


class SymlinkHookBatchTests(unittest.TestCase):
    def test_hook_is_a_no_op_when_off(self):
        with mock.patch.object(nw, 'enabled', return_value=False), \
                mock.patch.object(nw.threading, 'Thread') as thread:
            nw.write_for_symlink(5, '/x.mkv')
        thread.assert_not_called()

    def test_batch_writes_each_show_once(self):
        rows = [{'id': i, 'imdb_id': 'tt1', 'location_on_disk': f'/lib/S/Season 01/e{i}.mkv'} for i in (1, 2, 3)]

        class Conn:
            def execute(self, sql, params):
                return mock.Mock(fetchall=lambda: [(i, 'tt1', 'episode') for i in params])

            def close(self):
                pass

        with mock.patch('database.core.get_db_connection', return_value=Conn()), \
                mock.patch.object(nw, '_episode_rows', return_value=rows), \
                mock.patch.object(nw, 'write_show', return_value=nw._empty_counts()) as write:
            nw._write_batch({r['id']: r['location_on_disk'] for r in rows})
        write.assert_called_once()
        self.assertEqual(len(write.call_args.kwargs['only_paths']), 3)


if __name__ == '__main__':
    unittest.main()
