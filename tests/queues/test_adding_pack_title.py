"""Tests for resolve_torrent_title in queues.adding_queue."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_resolve_torrent_title():
    """Load resolve_torrent_title without triggering the routes/debrid import cycle."""
    path = os.path.join(ROOT, 'queues/adding_queue.py')
    source = open(path, encoding='utf-8').read()
    start = source.index('_PACK_FILE_EXTENSIONS = (')
    end = source.index('\ndef accepts_uncached_now(')
    ns = {}
    header = "import os\nfrom typing import Dict, List, Optional, Any\n"
    exec(header + source[start:end], ns)
    return ns['resolve_torrent_title']


resolve_torrent_title = _load_resolve_torrent_title()


def test_pack_named_after_one_file_uses_common_folder():
    folder = 'Detectorists.S01.1080p.WEBRip.Rus.Eng.Ozz'
    torrent_info = {
        'title': 'Detectorists.S01E04.1080p.WEBRip.Rus.Eng.Ozz.mkv',
        'files': [
            {'path': f'/{folder}/Detectorists.S01E0{i}.1080p.WEBRip.Rus.Eng.Ozz.mkv'}
            for i in range(1, 7)
        ],
    }
    chosen = {'title': 'Detectorists S01 1080p WEBRip'}
    assert resolve_torrent_title(torrent_info, chosen) == folder


def test_pack_named_after_file_without_common_folder_uses_scraped_title():
    torrent_info = {
        'title': 'Detectorists.S01E04.1080p.WEBRip.Rus.Eng.Ozz.mkv',
        'files': [
            {'path': f'Detectorists.S01E0{i}.1080p.WEBRip.Rus.Eng.Ozz.mkv'}
            for i in range(1, 7)
        ],
    }
    chosen = {'title': 'Detectorists.S01.1080p.WEBRip.Rus.Eng.Ozz'}
    assert resolve_torrent_title(torrent_info, chosen) == 'Detectorists.S01.1080p.WEBRip.Rus.Eng.Ozz'


def test_pack_named_after_file_without_common_folder_or_scraped_falls_back():
    provider_name = 'Detectorists.S01E04.1080p.WEBRip.Rus.Eng.Ozz.mkv'
    torrent_info = {
        'title': provider_name,
        'files': [
            {'path': 'Detectorists.S01E01.mkv'},
            {'path': 'Detectorists.S01E04.mkv'},
        ],
    }
    assert resolve_torrent_title(torrent_info, None) == provider_name


def test_pack_named_after_zip_uses_common_folder():
    folder = 'Show.S01.1080p.WEB-DL'
    torrent_info = {
        'title': 'Show.S01.1080p.WEB-DL.zip',
        'files': [
            {'path': f'{folder}/Show.S01E01.mkv'},
            {'path': f'{folder}/Show.S01E02.mkv'},
        ],
    }
    assert resolve_torrent_title(torrent_info, {'title': 'Scraped'}) == folder


def test_single_file_torrent_keeps_provider_name():
    provider_name = 'Detectorists.S01E04.1080p.WEBRip.Rus.Eng.Ozz.mkv'
    torrent_info = {
        'title': provider_name,
        'files': [
            {'path': f'Detectorists.S01/{provider_name}'},
        ],
    }
    chosen = {'title': 'Detectorists S01E04'}
    assert resolve_torrent_title(torrent_info, chosen) == provider_name
