"""Sampled-audio subtitle sync: offset, frame-rate fix, in-sync and refusal."""
import array
import math
import random
import shutil
import subprocess

import pytest

pytest.importorskip('pysubs2')
if not shutil.which('ffmpeg'):
    pytest.skip('ffmpeg not installed', allow_module_level=True)

from utilities import subtitle_sync  # noqa: E402

RATE = 8000
DUR = 600


def _srt(lines):
    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return ''.join(f"{i + 1}\n{ts(a)} --> {ts(b)}\nline {i}\n\n" for i, (a, b) in enumerate(lines)).encode()


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    rnd = random.Random(11)
    speech, t = [], 4.0
    while t < DUR - 4:
        length = rnd.uniform(0.8, 4.0)
        speech.append((t, t + length))
        t += length + rnd.uniform(0.4, 3.5)
    noise = array.array('h', (rnd.randint(-200, 200) for _ in range(997)))
    tone = array.array('h', (int(6000 * math.sin(2 * math.pi * k / 40) + 2500 * math.sin(2 * math.pi * k / 13))
                             for k in range(520)))
    buf = array.array('h', (noise[i % 997] for i in range(RATE * DUR)))
    for a, b in speech:
        for i in range(int(a * RATE), int(b * RATE)):
            buf[i] = buf[i] + tone[i % 520]
    path = tmp_path_factory.mktemp('sync') / 'a.mka'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', '-',
                    '-c:a', 'flac', str(path)], input=buf.tobytes(), check=True)
    return str(path), speech


@pytest.fixture(autouse=True)
def _dur(monkeypatch):
    monkeypatch.setattr(subtitle_sync, '_duration', lambda p: float(DUR))


def test_in_sync_left_alone(media):
    path, speech = media
    new, info = subtitle_sync.sync_subtitle(path, _srt(speech))
    assert new is None and info['status'] == 'in_sync'


def test_late_subtitle_shifted_back(media):
    path, speech = media
    new, info = subtitle_sync.sync_subtitle(path, _srt([(a + 3.4, b + 3.4) for a, b in speech]))
    assert info['status'] == 'synced' and abs(info['offset'] + 3.4) < 0.1 and info['ratio'] == 1.0
    import pysubs2
    first = pysubs2.SSAFile.from_string(new.decode()).events[0]
    assert abs(first.start / 1000 - speech[0][0]) < 0.1


def test_pal_speedup_fixed(media):
    path, speech = media
    k = 25 / 23.976
    new, info = subtitle_sync.sync_subtitle(path, _srt([(a / k - 1.5, b / k - 1.5) for a, b in speech if a / k > 2]))
    assert info['status'] == 'synced'
    assert abs(info['ratio'] - k) < 1e-4 and abs(info['offset'] - 1.5 * k) < 0.12


def test_unrelated_subtitle_refused(media):
    path, _ = media
    rnd = random.Random(99)
    lines, t = [], 4.0
    while t < DUR - 4:
        length = rnd.uniform(0.8, 4.0)
        lines.append((t, t + length))
        t += length + rnd.uniform(0.4, 3.5)
    new, info = subtitle_sync.sync_subtitle(path, _srt(lines))
    assert new is None and info['status'] == 'unsure'


def test_unreadable_video_fails_cleanly(media, tmp_path):
    _, speech = media
    new, info = subtitle_sync.sync_subtitle(str(tmp_path / 'missing.mkv'), _srt(speech))
    assert new is None and info['status'] == 'failed'


def test_describe():
    assert subtitle_sync.describe({'status': 'synced', 'offset': -3.4, 'ratio': 1.0, 'before': 0.1, 'after': 0.5}) \
        .startswith('synced: shifted -3.40s')
    assert subtitle_sync.describe({'status': 'in_sync'}) == 'already in sync'


@pytest.fixture(scope='module')
def silent_mkv_with_subs(tmp_path_factory, media):
    """Silent audio + an embedded SRT track: any fix must come from the track."""
    _, speech = media
    d = tmp_path_factory.mktemp('emb')
    (d / 'ref.srt').write_bytes(_srt(speech))
    out = d / 't.mkv'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=black:s=32x32:r=10:d={DUR}',
                    '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-i', str(d / 'ref.srt'),
                    '-map', '0', '-map', '1', '-map', '2', '-t', str(DUR), '-c:v', 'mpeg4', '-g', '20',
                    '-c:a', 'pcm_s16le', '-c:s', 'srt', str(out)], check=True)
    return str(out), speech


def test_embedded_track_reference(silent_mkv_with_subs):
    path, speech = silent_mkv_with_subs
    new, info = subtitle_sync.sync_subtitle(path, _srt([(a + 2.7, b + 2.7) for a, b in speech]))
    assert info['status'] == 'synced' and info['reference'].startswith('embedded')
    assert abs(info['offset'] + 2.7) < 0.1
    _, info = subtitle_sync.sync_subtitle(path, _srt(speech))
    assert info['status'] == 'in_sync'


def test_falls_back_to_audio_without_text_track(media):
    path, speech = media
    _, info = subtitle_sync.sync_subtitle(path, _srt([(a + 1.0, b + 1.0) for a, b in speech]))
    assert info['reference'] == 'audio' and info['status'] == 'synced'
