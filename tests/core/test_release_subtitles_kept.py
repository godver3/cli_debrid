"""Subtitles that come with a release (symlinked or regular sidecar files) must
never be deleted or overwritten by Clyde. Clyde only adds new subtitle files
beside them and only modifies/removes subtitles it recorded writing itself.
"""
import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip('subliminal')


@pytest.fixture
def sub_db(monkeypatch, tmp_path):
    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    monkeypatch.setenv('USER_DB_CONTENT', str(db_dir))
    import database  # noqa: F401
    from database import subtitle_history
    monkeypatch.setattr(subtitle_history, '_ready', False)
    conn = sqlite3.connect(db_dir / 'media_items.db')
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, tmdb_id TEXT,
        title TEXT, year INTEGER, state TEXT, type TEXT, episode_title TEXT, season_number INTEGER,
        episode_number INTEGER, collected_at TEXT, filled_by_file TEXT, filled_by_title TEXT,
        location_on_disk TEXT, version TEXT, ms_item_id TEXT, original_scraped_torrent_title TEXT,
        filled_by_torrent_id TEXT)""")
    conn.commit()
    conn.close()
    return subtitle_history


class _FakeSub:
    provider_name = 'opensubtitlescom'
    hearing_impaired = False

    def __init__(self, lang_code, content, fmt='srt'):
        from babelfish import Language
        self.language = Language.fromietf(lang_code)
        self.content = content
        self.subtitle_format = fmt

    def get_matches(self, video):
        return {'hash'}


def _ctx(video_path, item=None):
    from subliminal.video import Episode
    p = Path(video_path)
    return {
        'original_path': p,
        'rating_key': None,
        'video': Episode(p.name, 'Anime', 1, 1),
        'item': item or {'id': 1, 'title': 'Anime', 'type': 'episode', 'season_number': 1, 'episode_number': 1,
                         'filled_by_file': p.name},
    }


def test_save_subtitles_keeps_release_srt_and_symlinked_ass(sub_db, tmp_path):
    """A regular release .srt and a symlinked release .ass with the same
    {stem}.{lang}{ext} survive a download; Clyde writes beside them using a
    name Plex/Jellyfin/Clyde still read as the same language, and never writes
    through a symlink."""
    from utilities import downsub
    from utilities.subtitle_lang import sidecar_lang

    lib = tmp_path / 'lib'
    release_dir = tmp_path / 'release'
    lib.mkdir()
    release_dir.mkdir()

    video = lib / 'Anime - S01E01.mkv'
    video.write_bytes(b'')

    # Regular release .srt
    rel_srt = lib / 'Anime - S01E01.en.srt'
    rel_srt.write_bytes(b'release english srt')

    # Symlinked release .ass (pointing into release_dir)
    real_ass = release_dir / 'dub_signs.ass'
    real_ass.write_bytes(b'release anime dub ass')
    link_ass = lib / 'Anime - S01E01.en.ass'
    os.symlink(real_ass, link_ass)

    ctx = _ctx(video)
    subs = [
        _FakeSub('en', b'clyde downloaded srt', fmt='srt'),
        _FakeSub('en', b'clyde downloaded ass', fmt='ass'),
    ]
    with mock.patch.object(downsub, '_direct_mount_subtitle_server', return_value=None), \
            mock.patch.object(downsub, '_final_format', side_effect=[('srt', '.srt'), ('ass', '.ass')]), \
            mock.patch.object(downsub, '_mark_release_match', return_value=None):
        saved = downsub.save_subtitles(ctx, subs, source='auto')

    assert saved == 2
    # Both release files are completely untouched (including symlink target)
    assert rel_srt.read_bytes() == b'release english srt'
    assert os.path.islink(link_ass)
    assert real_ass.read_bytes() == b'release anime dub ass'

    # Clyde added new files beside them with language before extension
    clyde_srt = lib / 'Anime - S01E01.clyde.en.srt'
    clyde_ass = lib / 'Anime - S01E01.clyde.en.ass'
    assert clyde_srt.read_bytes() == b'clyde downloaded srt'
    assert clyde_ass.read_bytes() == b'clyde downloaded ass'
    assert sidecar_lang('clyde.en')[0] == 'eng'

    # Re-writing a file Clyde itself wrote earlier is fine (updates in place)
    subs2 = [_FakeSub('en', b'clyde updated srt', fmt='srt')]
    with mock.patch.object(downsub, '_direct_mount_subtitle_server', return_value=None), \
            mock.patch.object(downsub, '_final_format', return_value=('srt', '.srt')), \
            mock.patch.object(downsub, '_mark_release_match', return_value=None):
        saved2 = downsub.save_subtitles(ctx, subs2, source='manual')

    assert saved2 == 1
    assert rel_srt.read_bytes() == b'release english srt'
    assert clyde_srt.read_bytes() == b'clyde updated srt'
    assert not (lib / 'Anime - S01E01.clyde2.en.srt').exists()


def test_save_subtitles_climount_avoids_colliding_with_release_file(sub_db, tmp_path):
    """cli_mount sidecar injection picks a non-colliding filename when a
    release subtitle with {stem}.{lang}{ext} already sits in the mount folder."""
    from utilities import downsub

    mount_dir = tmp_path / 'mount' / 'job-uuid'
    mount_dir.mkdir(parents=True)
    video = mount_dir / 'Anime - S01E01.mkv'
    video.write_bytes(b'')
    (mount_dir / 'Anime - S01E01.en.srt').write_bytes(b'release sub on mount')

    injected = []

    class _FakeClient:
        def inject_sidecar_file(self, folder, filename, content):
            injected.append((folder, filename, content))
            return True

    ctx = _ctx(video)
    subs = [_FakeSub('en', b'clyde injected srt', fmt='srt')]
    with mock.patch.object(downsub, '_direct_mount_subtitle_server', return_value=None), \
            mock.patch.object(downsub, '_final_format', return_value=('srt', '.srt')), \
            mock.patch.object(downsub, '_mark_release_match', return_value=None), \
            mock.patch('os.access', return_value=False), \
            mock.patch('utilities.builtin_engine.selected', return_value=False), \
            mock.patch('usenet.climount_client.get_climount_client', return_value=_FakeClient()):
        saved = downsub.save_subtitles(ctx, subs, source='auto')

    assert saved == 1
    assert injected == [('job-uuid', 'Anime - S01E01.clyde.en.srt', b'clyde injected srt')]


def test_resync_never_replaces_or_removes_release_sidecars(sub_db, tmp_path):
    """resync_item and needs_check leave release .srt and symlinked .ass alone,
    even when a hash match exists, while still replacing Clyde's own sidecars."""
    from utilities import subtitle_resync, downsub

    lib = tmp_path / 'lib'
    rel_src = tmp_path / 'rel'
    lib.mkdir()
    rel_src.mkdir()

    video = lib / 'Anime - S01E01.mkv'
    video.write_bytes(b'')

    rel_srt = lib / 'Anime - S01E01.en.srt'
    rel_srt.write_bytes(b'release regular srt')

    real_ass = rel_src / 'dub.ass'
    real_ass.write_bytes(b'release symlinked ass')
    rel_ass = lib / 'Anime - S01E01.ja.ass'
    os.symlink(real_ass, rel_ass)

    clyde_srt = lib / 'Anime - S01E01.es.srt'
    clyde_srt.write_bytes(b'old clyde spanish srt')
    sub_db.record(1, 'Anime S01E01', 'es', 'subdl', 800, 971, [], '', 'srt', 'sidecar', str(clyde_srt), 'auto')

    item = {'id': 1, 'location_on_disk': str(video), 'title': 'Anime', 'type': 'episode',
            'season_number': 1, 'episode_number': 1}
    better_es = _FakeSub('es', b'better clyde spanish srt', fmt='srt')
    ctx = {'hash': True, 'video': object(), 'item': item, 'original_path': video}

    settings = {('File Management', 'file_collection_management'): 'Symlinked/Local'}
    with mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d)), \
            mock.patch.object(downsub, 'resolve_item_path', return_value=str(video)), \
            mock.patch.object(downsub, 'prepare_video', return_value=ctx), \
            mock.patch.object(downsub, 'hash_matches', return_value=[better_es]), \
            mock.patch.object(downsub, 'release_matches', return_value=[]), \
            mock.patch.object(downsub, 'fetch_subtitle', return_value=True), \
            mock.patch.object(downsub, '_final_format', return_value=('srt', '.srt')), \
            mock.patch.object(downsub, '_record_history'):
        changed, checked, notes = subtitle_resync.resync_item(item)

    assert changed == 1 and checked == 1
    assert clyde_srt.read_bytes() == b'better clyde spanish srt'
    # Release files untouched
    assert rel_srt.read_bytes() == b'release regular srt'
    assert os.path.islink(rel_ass) and real_ass.read_bytes() == b'release symlinked ass'

    # Direct _hash_replace_sidecar on release files is also refused
    finder = mock.Mock()
    assert subtitle_resync._hash_replace_sidecar(item, str(rel_srt), finder) is None
    assert subtitle_resync._hash_replace_sidecar(item, str(rel_ass), finder) is None
    assert not finder.best.called

    # Item with only release sidecars does not need resync check
    clyde_srt.unlink()
    with mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: settings.get((s, k), d)):
        assert not subtitle_resync.needs_check(item)


def test_plex_delete_refuses_non_clyde_external_streams(sub_db, tmp_path):
    """plex_stream_of_item and plex_delete only allow removing Plex external
    streams Clyde recorded uploading (saved_via='plex'), never a release
    sidecar Plex picked up."""
    from utilities import subtitle_resync

    lib = tmp_path / 'lib'
    lib.mkdir()
    video = lib / 'Anime - S01E01.mkv'
    video.write_bytes(b'')
    # Release sidecar on disk in English
    (lib / 'Anime - S01E01.en.srt').write_bytes(b'release en')

    item = {'id': 1, 'title': 'Anime', 'type': 'episode', 'season_number': 1, 'episode_number': 1,
            'filled_by_file': video.name, 'location_on_disk': str(video), 'ms_item_id': '500'}

    # Record only a Spanish Plex upload by Clyde
    sub_db.record(1, 'Anime S01E01', 'es', 'subdl', 900, 971, [], '', 'srt',
                  'plex', None, 'auto', media_key=video.name)

    streams = [
        {'id': 10, 'key': '', 'language': 'en', 'external': False},                     # embedded
        {'id': 11, 'key': '/library/streams/11', 'language': 'en', 'external': True},  # release sidecar
        {'id': 12, 'key': '/library/streams/12', 'language': 'fr', 'external': True},  # unknown external
        {'id': 13, 'key': '/library/streams/13', 'language': 'es', 'external': True},  # Clyde Plex upload
    ]
    with mock.patch.object(subtitle_resync, 'plex_streams', return_value=streams), \
            mock.patch.object(subtitle_resync, 'item_rk', return_value='500'):
        assert subtitle_resync.plex_stream_of_item(item, 10) is None
        assert subtitle_resync.plex_stream_of_item(item, 11) is None
        assert subtitle_resync.plex_stream_of_item(item, 12) is None
        assert subtitle_resync.plex_stream_of_item(item, 13)['id'] == 13

    # Direct plex_delete with item= also refuses non-Clyde streams
    with mock.patch('requests.delete') as req_del:
        ok, code = subtitle_resync.plex_delete(streams[1], item=item)
        assert not ok and code == 403
        ok2, code2 = subtitle_resync.plex_delete(streams[2], item=item)
        assert not ok2 and code2 == 403
        assert not req_del.called


def test_orphan_and_video_delete_cleanup_keep_release_subtitles(sub_db, tmp_path):
    """Both find_orphan_subtitles and remove_sidecar_subtitles skip release
    subtitles (regular and symlinked) and only remove Clyde's own."""
    from utilities.deletion_manager import find_orphan_subtitles, remove_sidecar_subtitles

    folder = tmp_path / 'Anime (2024)' / 'Season 01'
    rel_src = tmp_path / 'release'
    folder.mkdir(parents=True)
    rel_src.mkdir()

    video = folder / 'Anime - S01E01.mkv'
    video.write_bytes(b'')

    # Sidecars matching the video: release regular .srt, release symlink .ass, Clyde .es.srt
    rel_srt = folder / 'Anime - S01E01.en.srt'
    rel_srt.write_bytes(b'release srt')
    real_ass = rel_src / 'ep1.ass'
    real_ass.write_bytes(b'release ass')
    rel_ass = folder / 'Anime - S01E01.en.ass'
    os.symlink(real_ass, rel_ass)

    clyde_srt = folder / 'Anime - S01E01.clyde.en.srt'
    clyde_srt.write_bytes(b'clyde srt')
    sub_db.record(1, 'Anime S01E01', 'en', 'subdl', 900, 971, [], '', 'srt', 'sidecar', str(clyde_srt), 'auto')

    # Orphan sidecars (no matching video): release regular, release symlink, Clyde regular
    orphan_rel_srt = folder / 'Old.Release.en.srt'
    orphan_rel_srt.write_bytes(b'old release srt')
    orphan_rel_ass = folder / 'Old.Release.en.ass'
    os.symlink(real_ass, orphan_rel_ass)

    orphan_clyde = folder / 'Old.Release.clyde.en.srt'
    orphan_clyde.write_bytes(b'old clyde srt')
    sub_db.record(2, 'Anime S01E01', 'en', 'subdl', 900, 971, [], '', 'srt', 'sidecar', str(orphan_clyde), 'auto')

    # Orphan scan finds ONLY Clyde's orphan
    orphans = find_orphan_subtitles(str(tmp_path / 'Anime (2024)'))
    assert orphans == [str(orphan_clyde)]

    # Video delete removes ONLY Clyde's sidecar for that video
    removed = remove_sidecar_subtitles(str(video), check_show_nfo=False)
    assert removed == [str(clyde_srt)]
    assert not clyde_srt.exists()
    for kept in (rel_srt, rel_ass, orphan_rel_srt, orphan_rel_ass, orphan_clyde):
        assert os.path.lexists(kept), f"should have been kept: {kept}"
