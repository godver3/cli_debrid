import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

# Loaded by path: importing the debrid package pulls in the whole app.
_spec = importlib.util.spec_from_file_location(
    'torbox_zip_index', Path(__file__).resolve().parents[2] / 'debrid' / 'torbox' / 'zip_index.py')
zip_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zip_index)


class _Resp:
    def __init__(self, status, content):
        self.status_code, self.content = status, content


def _serve(monkeypatch, blob: bytes):
    calls = []

    def get(self, url, headers=None, timeout=None):
        a, b = headers['Range'][len('bytes='):].split('-')
        calls.append((int(a), int(b)))
        return _Resp(206, blob[int(a):int(b) + 1])

    monkeypatch.setattr(zip_index.requests.Session, 'get', get)
    return calls


def _zip(members, streamed=False):
    buf = io.BytesIO()
    target = buf
    if streamed:  # a non-seekable writer sets data descriptors (flag bit 3)
        class NoSeek(io.RawIOBase):
            def writable(self):
                return True

            def write(self, b):
                return buf.write(b)
        target = NoSeek()
    with zipfile.ZipFile(target, 'w') as z:
        z.writestr(zipfile.ZipInfo('Pack/'), b'')
        for name, data, method in members:
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            z.writestr(info, data)
    return buf.getvalue()


EPS = [('Pack/S01/Show.S01E01.mkv', b'A' * 5000, zipfile.ZIP_STORED),
       ('Pack/S01/Show.S01E02.mkv', b'B' * 7000, zipfile.ZIP_STORED),
       ('Pack/S01/Show.S01E01.srt', b'subs', zipfile.ZIP_STORED),
       ('Pack/notes.nfo', b'x' * 900, zipfile.ZIP_DEFLATED)]


@pytest.mark.parametrize('streamed', [False, True])
def test_read_index_gives_each_stored_member_its_bytes(monkeypatch, streamed):
    blob = _zip(EPS, streamed=streamed)
    calls = _serve(monkeypatch, blob)
    got = {m['path']: m for m in zip_index.read_index('http://cdn/z', len(blob))}
    assert set(got) == {'Pack/S01/Show.S01E01.mkv', 'Pack/S01/Show.S01E02.mkv', 'Pack/S01/Show.S01E01.srt'}
    for name, data, _ in EPS[:3]:
        m = got[name]
        assert m['size'] == len(data)
        assert blob[m['offset']:m['offset'] + m['size']] == data
    if not streamed:
        # tail + directory + 3 spot-checked local headers, not one per member
        assert len(calls) <= 5


def test_expand_keeps_zip_folder_and_id():
    zf = {'id': 7, 'path': 'Root/Root.zip', 'bytes': 999}
    out = zip_index.expand(zf, [{'path': 'Pack/S01/Show.S01E01.mkv', 'size': 10, 'offset': 40}])
    assert out == [{'id': 7, 'path': 'Root/Pack/S01/Show.S01E01.mkv', 'name': 'Show.S01E01.mkv', 'bytes': 10,
                    'selected': True, 'zip_offset': 40, 'zip_size': 999}]


def test_zip_only():
    assert zip_index.zip_only([{'path': 'a/b.ZIP', 'bytes': 5}])
    assert not zip_index.zip_only([{'path': 'a/b.zip', 'bytes': 5}, {'path': 'a/c.mkv', 'bytes': 5}])
    assert not zip_index.zip_only([{'path': 'a/b.mkv', 'bytes': 5}])


def test_index_is_built_once_and_stored(monkeypatch, tmp_path):
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path))
    monkeypatch.setattr(zip_index, '_mem', {})
    blob = _zip(EPS[:2])
    _serve(monkeypatch, blob)
    minted = []

    def url():
        minted.append(1)
        return 'http://cdn/z'
    first = zip_index.index('ABC', 1, len(blob), url)
    monkeypatch.setattr(zip_index, '_mem', {})  # a restart: read back from disk
    assert zip_index.index('abc', 1, len(blob), url) == first
    assert len(first) == 2 and minted == [1]


def test_unreadable_zip_gives_none(monkeypatch, tmp_path):
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path))
    monkeypatch.setattr(zip_index, '_mem', {})
    _serve(monkeypatch, b'not a zip at all' * 10)
    assert zip_index.index('h', 1, 160, lambda: 'http://cdn/z') is None
