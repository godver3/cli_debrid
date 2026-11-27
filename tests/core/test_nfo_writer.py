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
            mock.patch.object(nw, 'enabled', return_value=True),
            mock.patch.object(nw, 'symlinked_mode', return_value=True),
            mock.patch.object(nw, 'mode', return_value='all'),
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
        # ids only: Plex Series keeps the show's own title, artwork and cast
        self.assertIsNone(show.find('title'))
        self.assertIsNone(show.find('plot'))
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

    def test_off_without_nfos_of_ours_deletions_touch_nothing(self):
        # A user's own NFO stays, and no folder walk happens
        mine = self.rows[0]['location_on_disk'][:-4] + '.nfo'
        with open(mine, 'w') as f:
            f.write('<episodedetails/>')
        with mock.patch.object(nw, 'enabled', return_value=False), \
                mock.patch.object(nw, '_videos_in') as walk:
            self.assertEqual(nw.remove_for_video(self.rows[0]['location_on_disk']), [])
        walk.assert_not_called()
        self.assertTrue(os.path.exists(mine))

    def test_plex_mode_deletions_touch_nothing(self):
        nw.write_show('tt2396135')
        nfo = self.rows[0]['location_on_disk'][:-4] + '.nfo'
        with mock.patch.object(nw, 'symlinked_mode', return_value=False):
            self.assertEqual(nw.remove_for_video(self.rows[0]['location_on_disk']), [])
        self.assertTrue(os.path.exists(nfo))

    def test_legacy_marker_counts_as_ours(self):
        nfo = self.rows[0]['location_on_disk'][:-4] + '.nfo'
        with open(nfo, 'w') as f:
            f.write('<?xml version="1.0"?>\n<!-- written by cli_debrid -->\n<episodedetails/>')
        nw.write_show('tt2396135')
        self.assertIn(nw.MARKER, self._read(nfo))

    def test_off_means_order_change_writes_nothing(self):
        with mock.patch.object(nw, 'enabled', return_value=False), \
                mock.patch.object(nw, 'write_show') as write:
            self.assertEqual(nw.refresh_show('tt2396135'), '')
        write.assert_not_called()

    def test_changed_mode_only_writes_shows_with_a_chosen_order(self):
        with mock.patch.object(nw, 'mode', return_value='changed'), \
                mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None):
            c = nw.write_show('tt2396135')
        self.assertEqual(c['written'], 0)
        self.assertFalse(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))
        with mock.patch.object(nw, 'mode', return_value='changed'), \
                mock.patch('cli_battery.app.episode_order.show_episode_order', return_value='tvdb:dvd'):
            self.assertEqual(nw.write_show('tt2396135')['written'], 3)

    def test_changed_mode_back_to_default_order_removes_the_shows_nfos(self):
        nw.write_show('tt2396135')                       # 'all' mode: written
        with mock.patch.object(nw, 'mode', return_value='changed'), \
                mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None):
            note = nw.refresh_show('tt2396135')
        self.assertIn('removed (3)', note)
        self.assertEqual([f for d, _, fs in os.walk(self.show) for f in fs if f.endswith('.nfo')], [])

    def test_hook_skips_shows_the_mode_leaves_out(self):
        with mock.patch.object(nw, 'mode', return_value='changed'), \
                mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None):
            c = nw.write_show('tt2396135', only_paths=[self.rows[0]['location_on_disk']])
        self.assertEqual((c['written'], c['removed']), (0, 0))

    def test_control_characters_are_dropped(self):
        xml = nw.build_episode_nfo([{'season': 1, 'episode': 1, 'title': 'a\x01b'}], 'S')
        self.assertEqual(_parse_episodes(xml)[0].findtext('title'), 'ab')


class ModeTests(unittest.TestCase):
    def _mode(self, settings, symlinked=True):
        get = lambda section, key, default=None: settings.get((section, key), default)
        with mock.patch.object(nw, 'get_setting', side_effect=lambda s, k, d=None: (
                'Symlinked/Local' if symlinked else 'Plex') if (s, k) == ('File Management', 'file_collection_management')
                else get(s, k, d)):
            return nw.mode(), nw.enabled()

    def test_modes(self):
        self.assertEqual(self._mode({}), ('off', False))
        self.assertEqual(self._mode({('NFO', 'mode'): 'changed'}), ('changed', True))
        self.assertEqual(self._mode({('NFO', 'mode'): 'all'}), ('all', True))
        self.assertEqual(self._mode({('NFO', 'enabled'): True}), ('all', True))   # first version's switch
        self.assertEqual(self._mode({('NFO', 'mode'): 'all'}, symlinked=False), ('all', False))


class PlanTests(unittest.TestCase):
    """Aligned episodes get a minimal NFO; moved ones the right episode's details."""
    TM = {(5, 3): {'name': 'Gohan the Teacher!', 'air_date': '2014-04-20', 'still_path': '/wrong.jpg'},
          (6, 1): {'name': "Videl's Crisis? Gohan's Urgent Call-out!", 'air_date': '2014-06-01',
                   'still_path': '/right.jpg', 'vote_average': 8.1, 'vote_count': 12,
                   'crew': [{'job': 'Director', 'department': 'Directing', 'name': 'Togo Shoji'},
                            {'job': 'Screenplay', 'department': 'Writing', 'name': 'W. Riter'}],
                   'guest_stars': [{'name': 'Some Actor', 'character': 'Videl', 'profile_path': '/p.jpg', 'order': 0}]},
          (1, 1): {'name': 'Prologue to Battle!', 'air_date': '2009-04-05'}}

    def _plans(self, eps, tm=TM):
        from utilities import nfo_sources as ns
        with mock.patch.object(ns, 'tmdb_episodes', return_value=tm), \
                mock.patch.object(ns, 'tvdb_episode', return_value={'image': 'https://tvdb/img.jpg', 'characters': []}):
            return ns.plan_episodes(eps, 61709)

    def test_aligned_episode_is_minimal(self):
        p = self._plans([{'season': 1, 'episode': 1, 'title': 'Prologue to Battle!', 'aired': '2009-04-05'}])
        self.assertEqual(p[0]['mode'], 'minimal')
        xml = nw.build_episode_nfo([{'season': 1, 'episode': 1, 'title': 'x', 'tvdb_id': 5}], 'S', [p[0]])
        ep = _parse_episodes(xml)[0]
        self.assertIsNone(ep.find('title'))
        self.assertEqual((ep.findtext('season'), ep.findtext('episode')), ('1', '1'))

    def test_moved_episode_gets_the_right_tmdb_episode_details(self):
        ep = {'season': 5, 'episode': 3, 'title': "Videl's Crisis? Gohan's Urgent Call-out!", 'aired': '2014-06-01'}
        plan = self._plans([ep])[0]
        self.assertEqual(plan['mode'], 'full')
        self.assertEqual(plan['thumb'], 'https://image.tmdb.org/t/p/original/right.jpg')
        self.assertEqual(plan['directors'], ['Togo Shoji'])
        x = _parse_episodes(nw.build_episode_nfo([ep], 'Dragon Ball Kai', [plan]))[0]
        self.assertEqual(x.findtext('title'), ep['title'])
        self.assertEqual(x.findtext('thumb'), 'https://image.tmdb.org/t/p/original/right.jpg')
        self.assertEqual(x.findtext('credits'), 'W. Riter')
        self.assertEqual(x.find('actor').findtext('role'), 'Videl')
        self.assertIsNone(x.find('ratings'))   # Plex mixes ratings up with a custom agent

    def test_moved_episode_missing_from_tmdb_uses_tvdb_image(self):
        plan = self._plans([{'season': 4, 'episode': 21, 'title': 'Peace for the Future!', 'aired': '2011-03-27',
                             'tvdb_id': 99}])[0]
        self.assertEqual((plan['mode'], plan['thumb']), ('full', 'https://tvdb/img.jpg'))

    def test_without_tmdb_everything_is_full(self):
        p = self._plans([{'season': 1, 'episode': 1, 'title': 'Prologue to Battle!'}], tm=None)
        self.assertEqual(p[0], {'mode': 'full'})


class SymlinkHookBatchTests(unittest.TestCase):
    def test_hook_is_a_no_op_when_off(self):
        with mock.patch.object(nw, 'enabled', return_value=False), \
                mock.patch('utilities.plex_snapshot.carry_to_symlink') as carry, \
                mock.patch.object(nw.threading, 'Thread') as thread:
            nw.write_for_symlink(5, '/x.mkv')
        thread.assert_not_called()
        carry.assert_called_once_with(5, '/x.mkv')

    def test_batch_writes_each_show_once(self):
        rows = [{'id': i, 'imdb_id': 'tt1', 'location_on_disk': f'/lib/S/Season 01/e{i}.mkv'} for i in (1, 2, 3)]

        class Conn:
            def execute(self, sql, params):
                return mock.Mock(fetchall=lambda: [(i, 'tt1', 'episode') for i in params])

            def close(self):
                pass

        with mock.patch('database.core.get_db_connection', return_value=Conn()), \
                mock.patch.object(nw, '_episode_rows', return_value=rows), \
                mock.patch.object(nw, 'wants_show', return_value=True), \
                mock.patch.object(nw, 'write_show', return_value=nw._empty_counts()) as write:
            nw._write_batch({r['id']: r['location_on_disk'] for r in rows})
        write.assert_called_once()
        self.assertEqual(len(write.call_args.kwargs['only_paths']), 3)


class TvdbEpisodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        from utilities import nfo_sources as ns
        self.ns = ns
        self._orig_mem = ns._tvdb_mem
        ns._tvdb_mem = None
        p = mock.patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._reset_mem)

    def _reset_mem(self):
        self.ns._tvdb_mem = self._orig_mem

    def test_error_and_unconfigured_are_not_persisted_and_retried(self):
        cache_path = os.path.join(self.tmp, 'nfo_tvdb_episodes.json')
        # TVDB not configured: returns None, nothing persisted
        with mock.patch('cli_battery.app.tvdb_client.is_available', return_value=False):
            self.assertIsNone(self.ns.tvdb_episode(42))
        self.assertFalse(os.path.exists(cache_path))
        self.assertNotIn('42', self.ns._tvdb_mem)

        # Transient error (_make_request returns None): returns None, nothing persisted
        with mock.patch('cli_battery.app.tvdb_client.is_available', return_value=True), \
                mock.patch('cli_battery.app.tvdb_client._make_request', return_value=None):
            self.assertIsNone(self.ns.tvdb_episode(42))
        self.assertFalse(os.path.exists(cache_path))
        self.assertNotIn('42', self.ns._tvdb_mem)

        # Later when TVDB works again: retried, returned, and persisted
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'data': {'name': 'Pilot', 'overview': 'Desc', 'image': 'https://tvdb/1.jpg', 'characters': []}}
        with mock.patch('cli_battery.app.tvdb_client.is_available', return_value=True), \
                mock.patch('cli_battery.app.tvdb_client._make_request', return_value=resp) as req:
            rec = self.ns.tvdb_episode(42)
            self.assertEqual(rec['name'], 'Pilot')
            self.assertEqual(rec['image'], 'https://tvdb/1.jpg')
            self.assertTrue(os.path.exists(cache_path))
            # Cached on second call
            self.assertEqual(self.ns.tvdb_episode(42)['name'], 'Pilot')
            self.assertEqual(req.call_count, 1)

    def test_404_is_cached_on_disk(self):
        cache_path = os.path.join(self.tmp, 'nfo_tvdb_episodes.json')
        resp = mock.Mock(status_code=404)
        with mock.patch('cli_battery.app.tvdb_client.is_available', return_value=True), \
                mock.patch('cli_battery.app.tvdb_client._make_request', return_value=resp) as req:
            self.assertIsNone(self.ns.tvdb_episode(99))
            self.assertTrue(os.path.exists(cache_path))
            self.assertIsNone(self.ns.tvdb_episode(99))
            self.assertEqual(req.call_count, 1)


class ResyncNfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_root = os.path.join(self.tmp, 'orig')
        self.sym_root = os.path.join(self.tmp, 'symlinks')
        os.makedirs(self.orig_root)
        self.src = os.path.join(self.orig_root, 'Shetland.S01E01.mkv')
        with open(self.src, 'wb') as f:
            f.write(b'video')
        self.old_show = os.path.join(self.sym_root, 'TV Shows', 'Shetland')
        self.old_s1 = os.path.join(self.old_show, 'Season 01')
        os.makedirs(self.old_s1)
        self.old_link = os.path.join(self.old_s1, 'Shetland - S01E01.mkv')
        os.symlink(self.src, self.old_link)
        self.new_show = os.path.join(self.sym_root, 'TV Shows', 'Shetland (2013)')
        self.new_link = os.path.join(self.new_show, 'Season 01', 'Shetland (2013) - S01E01.mkv')
        self.item = {
            'id': 1, 'title': 'Shetland', 'type': 'episode',
            'location_on_disk': self.old_link,
            'original_path_for_symlink': self.src,
            'filled_by_file': os.path.basename(self.src),
        }

    def _run_resync(self):
        from utilities import local_library_scan as lls
        settings = {
            ('File Management', 'file_collection_management'): 'Symlinked/Local',
            ('File Management', 'original_files_path'): self.orig_root,
            ('File Management', 'symlinked_files_path'): self.sym_root,
        }
        with mock.patch.object(lls, 'get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d)), \
                mock.patch.object(nw, 'get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d)), \
                mock.patch.object(lls, 'get_all_media_items',
                                  side_effect=lambda state=None: [dict(self.item)] if state == 'Collected' else []), \
                mock.patch.object(lls, 'get_symlink_path', return_value=self.new_link), \
                mock.patch.object(lls, 'update_media_item'), \
                mock.patch.object(nw, 'write_for_symlink'):
            return lls.resync_symlinks_with_new_settings()

    def test_resync_removes_old_clyde_nfos_and_prunes_old_folder(self):
        old_ep_nfo = self.old_link[:-4] + '.nfo'
        old_show_nfo = os.path.join(self.old_show, 'tvshow.nfo')
        with open(old_ep_nfo, 'w', encoding='utf-8') as f:
            f.write(f'<?xml version="1.0"?>\n{nw.MARKER}\n<episodedetails/>')
        with open(old_show_nfo, 'w', encoding='utf-8') as f:
            f.write(f'<?xml version="1.0"?>\n{nw.MARKER}\n<tvshow/>')
        res = self._run_resync()
        self.assertEqual(res['symlinks_updated_count'], 1)
        self.assertTrue(os.path.islink(self.new_link))
        self.assertFalse(os.path.exists(old_ep_nfo))
        self.assertFalse(os.path.exists(old_show_nfo))
        self.assertFalse(os.path.exists(self.old_show))  # empty folder pruned

    def test_resync_keeps_user_nfo(self):
        user_nfo = self.old_link[:-4] + '.nfo'
        with open(user_nfo, 'w', encoding='utf-8') as f:
            f.write('<episodedetails><title>mine</title></episodedetails>')
        res = self._run_resync()
        self.assertEqual(res['symlinks_updated_count'], 1)
        self.assertTrue(os.path.exists(user_nfo))


if __name__ == '__main__':
    unittest.main()
