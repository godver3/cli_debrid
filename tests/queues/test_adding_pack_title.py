"""Tests for resolve_torrent_title in debrid/common/utils.py."""
import importlib.util
from pathlib import Path

_UTILS_PATH = Path(__file__).resolve().parents[2] / 'debrid' / 'common' / 'utils.py'
_spec = importlib.util.spec_from_file_location('debrid_common_utils_pack_title', _UTILS_PATH)
_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_utils)
resolve_torrent_title = _utils.resolve_torrent_title


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


def test_accepts_filename_key_when_title_absent():
    folder = 'Pokemon.S01.1080p.WEBRip'
    torrent_info = {
        'filename': 'Pokemon.S01E75.1080p.WEBRip.mkv',
        'files': [
            {'path': f'{folder}/Pokemon.S01E01.mkv'},
            {'path': f'{folder}/Pokemon.S01E75.mkv'},
        ],
    }
    assert resolve_torrent_title(torrent_info) == folder
