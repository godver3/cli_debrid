"""Plex metadata snapshot -> NFO files (utilities/plex_snapshot.py)."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from utilities import nfo_writer as nw
from utilities import plex_snapshot as ps

SHOWS_XML = """<MediaContainer size="1" totalSize="1">
<Directory ratingKey="100" title="Shetland" year="2013" summary="Show plot">
  <Guid id="tvdb://263133"/><Guid id="tmdb://46923"/><Guid id="imdb://tt2396135"/>
</Directory></MediaContainer>"""

EPISODES_XML = """<MediaContainer size="3" totalSize="3">
<Video ratingKey="101" grandparentRatingKey="100" grandparentTitle="Shetland" parentIndex="1" index="1"
       title="Red Bones (1)" summary="Plex plot one" originallyAvailableAt="2013-03-10" duration="3540000">
  <Media><Part file="/data/symlinks/TV Shows/Shetland (2013)/Season 01/Shetland (2013) - S01E01 - x.mkv"/></Media>
  <Guid id="tvdb://4500001"/><Director tag="Peter Hoar"/><Writer tag="Ann Cleeves"/>
</Video>
<Video ratingKey="102" grandparentRatingKey="100" grandparentTitle="Shetland" parentIndex="1" index="2"
       title="Red Bones (2)" summary="Plex plot two" originallyAvailableAt="2013-03-17">
  <Media><Part file="/data/symlinks/TV Shows/Shetland (2013)/Season 01/Shetland (2013) - S01E02 - x.mkv"/></Media>
</Video>
<Video ratingKey="999" grandparentRatingKey="200" grandparentTitle="Other" parentIndex="1" index="1" title="Not ours">
  <Media><Part file="/data/symlinks/TV Shows/Other/Season 01/Other - S01E01.mkv"/></Media>
</Video></MediaContainer>"""


def _q(_plex, path, method='get', params=None):
    kind = dict(params or []).get('type')
    return ET.fromstring(SHOWS_XML if kind == '2' else EPISODES_XML)


class SnapshotTests(unittest.TestCase):
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
                              'season_number': 1, 'episode_number': e, 'episode_title': f'db {e}',
                              'release_date': None, 'runtime': 60, 'location_on_disk': path})
        self.settings = {}
        section = mock.Mock(key='1', title='TV Shows')
        patches = [
            mock.patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp}),
            mock.patch.object(nw, 'library_root', return_value=self.root),
            mock.patch.object(nw, 'symlinked_mode', return_value=True),
            mock.patch.object(nw, 'mode', return_value='off'),
            mock.patch.object(nw, '_show_imdb_ids_in', return_value={'tt2396135'}),
            mock.patch.object(nw, '_episode_rows', side_effect=lambda imdb_id=None: [dict(r) for r in self.rows]),
            mock.patch.object(nw, '_episode_rows_by_id',
                              side_effect=lambda i: [dict(r) for r in self.rows if r['id'] == i]),
            mock.patch('utilities.plex_nfo_agent._server', return_value=mock.Mock(friendlyName='pms')),
            mock.patch('utilities.plex_nfo_agent.tv_sections', return_value=[section]),
            mock.patch('utilities.plex_nfo_agent._q', side_effect=_q),
            mock.patch.object(ps, 'get_setting', side_effect=lambda s, k, d=None: self.settings.get((s, k), d)),
            mock.patch.object(ps, 'set_setting', side_effect=lambda s, k, v: self.settings.__setitem__((s, k), v)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def nfo(self, e):
        return self.rows[e - 1]['location_on_disk'][:-4] + '.nfo'

    def _read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _take(self):
        sid = ps._take()
        self.settings[('NFO', 'snapshot_id')] = str(sid)
        return sid, ps._write_all(sid)

    def test_snapshot_writes_plex_details_as_nfo_files(self):
        sid, c = self._take()
        self.assertEqual(c['written'], 3)  # two episodes + tvshow.nfo
        body = self._read(self.nfo(1))
        self.assertIn(nw.SNAPSHOT_MARKER, body)
        self.assertIn('<title>Red Bones (1)</title>', body)
        self.assertIn('<plot>Plex plot one</plot>', body)
        self.assertIn('<aired>2013-03-10</aired>', body)
        self.assertIn('<runtime>59</runtime>', body)
        self.assertIn('<director>Peter Hoar</director>', body)
        self.assertIn('<uniqueid type="tvdb" default="true">4500001</uniqueid>', body)
        show = self._read(os.path.join(self.show, 'tvshow.nfo'))
        self.assertIn('<uniqueid type="tvdb" default="true">263133</uniqueid>', show)
        snap = ps.snapshots()[0]
        self.assertEqual((snap['id'], snap['files'], snap['shows'], snap['unmatched']), (sid, 2, 1, 1))
        self.assertEqual(ps._write_all(sid)['unchanged'], 3)

    def test_never_replaces_writer_or_user_nfos(self):
        with open(self.nfo(1), 'w') as f:
            f.write('<episodedetails>mine</episodedetails>')
        with open(self.nfo(2), 'w') as f:
            f.write(f'<?xml version="1.0"?>\n{nw.MARKER}\n<episodedetails/>')
        _sid, c = self._take()
        self.assertEqual(c['foreign'], 2)
        self.assertEqual(self._read(self.nfo(1)), '<episodedetails>mine</episodedetails>')

    def test_shows_the_nfo_mode_covers_are_left_to_it(self):
        with mock.patch.object(nw, 'wants_show', return_value=True):
            _sid, c = self._take()
        self.assertEqual((c['written'], c['writer_shows']), (0, 1))

    def test_writer_may_replace_snapshot_nfos(self):
        self._take()
        self.assertEqual(nw._write(self.nfo(1), 'x' + nw.MARKER), 'written')

    def test_turning_off_keeps_snapshot_nfos(self):
        self._take()
        self.assertEqual(nw.remove_library()['removed'], 0)
        self.assertTrue(os.path.exists(self.nfo(1)))

    def test_deleted_episode_takes_its_snapshot_nfo_along(self):
        self._take()
        os.unlink(self.rows[0]['location_on_disk'])
        nw.remove_for_video(self.rows[0]['location_on_disk'])
        self.assertFalse(os.path.exists(self.nfo(1)))
        self.assertTrue(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))

    def test_upgrade_keeps_the_snapshot_nfo(self):
        self._take()
        old = self.rows[0]['location_on_disk']
        new = os.path.join(self.s1, 'Shetland (2013) - S01E01 - 2160p.mkv')
        os.symlink('/nonexistent/other.mkv', new)
        os.unlink(old)
        nw.remove_for_video(old)
        self.rows[0]['location_on_disk'] = new
        self.assertTrue(ps.carry_to_symlink(1, new))
        self.assertIn('<title>Red Bones (1)</title>', self._read(new[:-4] + '.nfo'))
        # and the moved entry is what a later rewrite uses
        self.assertEqual(ps._write_all(int(self.settings[('NFO', 'snapshot_id')]))['unchanged'], 3)

    def test_renumbered_file_gets_no_snapshot_nfo(self):
        self._take()
        new = os.path.join(self.s1, 'Shetland (2013) - S01E05 - x.mkv')
        os.symlink('/nonexistent/other.mkv', new)
        self.assertFalse(ps.carry_to_symlink(1, new))
        self.rows[0]['episode_number'] = 5
        self.assertFalse(ps.carry_to_symlink(1, self.rows[0]['location_on_disk']))
        self.assertFalse(os.path.exists(new[:-4] + '.nfo'))

    def _upgrade_first_episode(self, name):
        self._take()
        old = self.rows[0]['location_on_disk']
        new = os.path.join(self.s1, name)
        os.symlink('/nonexistent/other.mkv', new)
        os.unlink(old)
        nw.remove_for_video(old)
        self.rows[0]['location_on_disk'] = new
        return new

    def test_upgrade_in_changed_mode_keeps_the_snapshot_nfo(self):
        # NFO files on ('changed'), the show on its default order: the
        # writer doesn't cover it, so the upgrade keeps the snapshot's NFO
        new = self._upgrade_first_episode('Shetland (2013) - S01E01 - 2160p.mkv')
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [(1, 'tt2396135', 'episode')]
        with mock.patch.object(nw, 'mode', return_value='changed'), \
                mock.patch.object(nw, 'wants_show', return_value=False), \
                mock.patch('database.core.get_db_connection', return_value=conn):
            nw._write_batch({1: new})
        self.assertIn('<title>Red Bones (1)</title>', self._read(new[:-4] + '.nfo'))

    def test_upgrade_with_a_name_without_sxxeyy_keeps_the_snapshot_nfo(self):
        new = self._upgrade_first_episode('Shetland 1x01 2160p.mkv')
        self.assertTrue(ps.carry_to_symlink(1, new))
        self.assertIn('<title>Red Bones (1)</title>', self._read(new[:-4] + '.nfo'))

    def test_file_episodes(self):
        self.assertEqual(ps._file_episodes('Show - S01E17 - x.mkv'), {(1, 17)})
        self.assertEqual(ps._file_episodes('Show - S01E17-E18 - x.mkv'), {(1, 17), (1, 18)})
        self.assertEqual(ps._file_episodes('Show.S01E17E18.mkv'), {(1, 17), (1, 18)})
        self.assertEqual(ps._file_episodes('Show.S01E17-1080p.mkv'), {(1, 17)})
        self.assertIsNone(ps._file_episodes('Show 1x17.mkv'))

    def test_two_libraries_on_one_folder_list_each_episode_once(self):
        sections = [mock.Mock(key='1', title='TV Shows'), mock.Mock(key='2', title='TV again')]
        with mock.patch('utilities.plex_nfo_agent.tv_sections', return_value=sections):
            self._take()
        self.assertEqual(self._read(self.nfo(1)).count('<episodedetails>'), 1)

    def test_nfo_shared_by_mkv_and_mp4_stays_while_one_is_left(self):
        self._take()
        mp4 = self.rows[0]['location_on_disk'][:-4] + '.mp4'
        os.symlink('/nonexistent/target.mp4', mp4)
        os.unlink(self.rows[0]['location_on_disk'])
        nw.remove_for_video(self.rows[0]['location_on_disk'])
        self.assertTrue(os.path.exists(self.nfo(1)))
        os.unlink(mp4)
        nw.remove_for_video(mp4)
        self.assertFalse(os.path.exists(self.nfo(1)))

    def test_no_snapshot_in_use_is_a_no_op(self):
        self.assertFalse(ps.carry_to_symlink(1, self.rows[0]['location_on_disk']))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'plex_metadata_snapshot.db')))

    def test_writer_dropping_a_show_brings_snapshot_nfos_back(self):
        self._take()
        with mock.patch.object(nw, 'wants_show', return_value=False), \
                mock.patch.object(nw, 'remove_show', return_value=2) as rm:
            os.remove(self.nfo(1))
            nw.write_show('tt2396135')
        rm.assert_called_once()
        self.assertIn(nw.SNAPSHOT_MARKER, self._read(self.nfo(1)))

    def test_remove_deletes_only_snapshot_nfos(self):
        self._take()
        mine = os.path.join(self.s1, 'user.nfo')
        with open(mine, 'w') as f:
            f.write('<x/>')
        self.assertEqual(ps._remove_all(), 3)
        self.assertTrue(os.path.exists(mine))

    def test_resync_removes_old_snapshot_nfo_and_carries_to_new_path(self):
        from utilities import local_library_scan as lls
        orig_root = os.path.join(self.tmp, 'orig')
        os.makedirs(orig_root)
        items = []
        new_show = os.path.join(self.root, 'TV Shows', 'Shetland [2013]')
        new_paths = {}
        for row in self.rows:
            src = os.path.join(orig_root, f'Shetland.S01E0{row["episode_number"]}.mkv')
            with open(src, 'wb') as f:
                f.write(b'video')
            os.unlink(row['location_on_disk'])
            os.symlink(src, row['location_on_disk'])
            new_path = os.path.join(new_show, 'Season 01', f'Shetland [2013] - S01E0{row["episode_number"]}.mkv')
            new_paths[row['id']] = new_path
            items.append({
                'id': row['id'], 'title': 'Shetland', 'type': 'episode',
                'location_on_disk': row['location_on_disk'],
                'original_path_for_symlink': src,
                'filled_by_file': os.path.basename(src),
            })
        self._take()
        self.assertTrue(os.path.exists(self.nfo(1)))
        self.assertTrue(os.path.exists(os.path.join(self.show, 'tvshow.nfo')))

        settings = {
            ('File Management', 'file_collection_management'): 'Symlinked/Local',
            ('File Management', 'original_files_path'): orig_root,
            ('File Management', 'symlinked_files_path'): self.root,
        }
        with mock.patch.object(lls, 'get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d)), \
                mock.patch.object(lls, 'get_all_media_items',
                                  side_effect=lambda state=None: [dict(x) for x in items] if state == 'Collected' else []), \
                mock.patch.object(lls, 'get_symlink_path', side_effect=lambda item, *_a, **_kw: new_paths[item['id']]), \
                mock.patch.object(lls, 'update_media_item'):
            res = lls.resync_symlinks_with_new_settings()
        self.assertEqual(res['symlinks_updated_count'], 2)
        self.assertFalse(os.path.exists(self.show))  # old NFOs gone, folder pruned
        new_ep1_nfo = new_paths[1][:-4] + '.nfo'
        self.assertIn(nw.SNAPSHOT_MARKER, self._read(new_ep1_nfo))
        self.assertIn('<title>Red Bones (1)</title>', self._read(new_ep1_nfo))
        self.assertTrue(os.path.exists(os.path.join(new_show, 'tvshow.nfo')))


class MatchPathTests(unittest.TestCase):
    def test_longest_folder_suffix_wins(self):
        by_name = {'e.mkv': ['/lib/A/Season 01/e.mkv', '/lib/B/Season 01/e.mkv']}
        self.assertEqual(ps.match_path('/plex/B/Season 01/e.mkv', by_name), ('/lib/B/Season 01/e.mkv', False))

    def test_tie_is_ambiguous(self):
        by_name = {'e.mkv': ['/lib/A/e.mkv', '/lib/B/e.mkv']}
        self.assertEqual(ps.match_path('/plex/C/e.mkv', by_name), (None, True))

    def test_windows_plex_paths(self):
        by_name = {'e.mkv': ['/lib/A/e.mkv']}
        self.assertEqual(ps.match_path('D:\\TV\\A\\e.mkv', by_name), ('/lib/A/e.mkv', False))


class PlexReadsNfosTests(unittest.TestCase):
    def test_order_change_leaves_plex_alone_when_files_are_only_written(self):
        from utilities import episode_order_change as eoc
        from utilities import plex_order_metadata as pom
        with mock.patch.object(nw, 'wants_show', return_value=True), \
                mock.patch('utilities.plex_nfo_agent.applied', return_value=False):
            self.assertFalse(pom._nfo_mode('tt1'))
            self.assertFalse(eoc._nfo_mode('tt1'))
        with mock.patch.object(nw, 'wants_show', return_value=True), \
                mock.patch('utilities.plex_nfo_agent.applied', return_value=True):
            self.assertTrue(pom._nfo_mode('tt1'))

    def test_applied_reads_saved_state(self):
        from utilities import plex_nfo_agent as pna
        with mock.patch.object(pna, '_load_state', return_value={'group_id': 9, 'previous': {'1': {}}}):
            self.assertTrue(pna.applied())
        with mock.patch.object(pna, '_load_state', return_value={'group_id': 9, 'previous': {}}):
            self.assertFalse(pna.applied())

    def test_leave_setting(self):
        with mock.patch.object(nw, 'get_setting', return_value='leave'):
            self.assertFalse(nw.switch_plex_agent())
        with mock.patch.object(nw, 'get_setting', return_value='switch'):
            self.assertTrue(nw.switch_plex_agent())


if __name__ == '__main__':
    unittest.main()
