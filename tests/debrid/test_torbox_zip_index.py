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
    def __init__(self, status, content, *, forbid_body_read=False):
        self.status_code = status
        self._data = content
        self._forbid_body_read = forbid_body_read
        self.closed = False
        self.body_read = False

    @property
    def content(self):
        self.body_read = True
        if self._forbid_body_read:
            raise AssertionError('response body must not be read')
        return self._data

    def iter_content(self, chunk_size=1):
        self.body_read = True
        if self._forbid_body_read:
            raise AssertionError('response body must not be read')
        step = max(1, chunk_size or len(self._data) or 1)
        for i in range(0, len(self._data), step):
            yield self._data[i:i + step]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _serve(monkeypatch, blob: bytes):
    calls = []

    def get(self, url, headers=None, timeout=None, stream=False):
        assert stream is True
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


def test_range_200_does_not_read_body_and_retries(monkeypatch):
    responses = []
    sleeps = []

    def get(self, url, headers=None, timeout=None, stream=False):
        assert stream is True
        resp = _Resp(200, b'X' * 1024, forbid_body_read=True)
        responses.append(resp)
        return resp

    monkeypatch.setattr(zip_index.requests.Session, 'get', get)
    monkeypatch.setattr(zip_index.time, 'sleep', sleeps.append)

    with pytest.raises(ValueError, match='answered 200'):
        zip_index._Range('http://cdn/z', 1000).read(0, 100)

    assert len(responses) == 3
    assert all(r.closed and not r.body_read for r in responses)
    assert sleeps == [1, 3, 5]


def test_range_206_reads_exact_bytes_and_closes(monkeypatch):
    served = []

    class _ChunkedResp(_Resp):
        def iter_content(self, chunk_size=1):
            self.body_read = True
            # Emit small chunks and extra trailing bytes past requested length.
            for part in (b'abc', b'defg', b'hijEXTRA'):
                yield part

    def get(self, url, headers=None, timeout=None, stream=False):
        assert stream is True
        resp = _ChunkedResp(206, b'')
        served.append(resp)
        return resp

    monkeypatch.setattr(zip_index.requests.Session, 'get', get)
    got = zip_index._Range('http://cdn/z', 1000).read(10, 10)
    assert got == b'abcdefghij'
    assert len(served) == 1 and served[0].closed


def test_range_206_short_read_raises_and_closes(monkeypatch):
    served = []

    def get(self, url, headers=None, timeout=None, stream=False):
        assert stream is True
        resp = _Resp(206, b'short')
        served.append(resp)
        return resp

    monkeypatch.setattr(zip_index.requests.Session, 'get', get)
    with pytest.raises(ValueError, match='returned 5 of 10 bytes'):
        zip_index._Range('http://cdn/z', 1000).read(0, 10)
    assert len(served) == 1 and served[0].closed
