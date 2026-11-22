"""Subtitle timing sync from sampled audio.

Fits a subtitle to the video with one offset and an optional frame-rate
ratio (23.976 <-> 24 <-> 25 fps releases), the way alass does without
splits. Unlike alass/ffsubsync it never decodes the whole audio track:
ffmpeg reads a handful of short windows spread through the file (input
seeking, so on a debrid/usenet mount only those parts are fetched), speech
is detected in them, and the subtitle lines are matched against that.

Pure Python (ffmpeg + pysubs2, both already required). A result is only
applied when it fits clearly better than the original timing.
"""
import array
import logging
import math
import os
import shutil
import subprocess
import sys

RATE = 8000                 # Hz, mono s16le from ffmpeg
FRAME = 160                 # 20 ms VAD frames
STEP = 0.02                 # offset grid, seconds
RATIOS = (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25)


def _duration(path):
    try:
        from usenet.repair_engine import _media_header
        info = _media_header(str(path))
        if info and info.get('duration'):
            return float(info['duration'])
    except Exception:
        pass
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=nw=1:nk=1', str(path)],
                           capture_output=True, text=True, timeout=20)
        return float(r.stdout.strip())
    except Exception:
        return None


def _read_window(path, start, length, timeout):
    """Band-limited mono PCM samples of [start, start+length), or None."""
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{start:.3f}', '-i', str(path),
           '-t', f'{length:.3f}', '-map', '0:a:0', '-vn', '-sn', '-dn', '-ac', '1',
           '-ar', str(RATE), '-af', 'highpass=f=200,lowpass=f=3500', '-f', 's16le', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.info(f"[subsync] audio read at {start:.0f}s timed out after {timeout}s")
        return None
    except Exception as e:
        logging.debug(f"[subsync] ffmpeg failed: {e}")
        return None
    if r.returncode != 0 or len(r.stdout) < RATE:
        return None
    samples = array.array('h')
    samples.frombytes(r.stdout[:len(r.stdout) - len(r.stdout) % 2])
    if sys.byteorder == 'big':
        samples.byteswap()
    return samples


def _speech_intervals(samples, start):
    """Voice-activity intervals (absolute seconds) in one window: frame
    energy above an adaptive floor, short gaps filled, blips dropped."""
    n = len(samples) // FRAME
    if n < 10:
        return []
    db = []
    for i in range(n):
        seg = samples[i * FRAME:(i + 1) * FRAME]
        e = sum(x * x for x in seg) / FRAME
        db.append(10 * math.log10(e + 1.0))
    srt = sorted(db)
    floor, mid, top = srt[n // 5], srt[n // 2], srt[int(n * 0.95)]
    if top - floor < 6:          # flat audio: silence or constant noise
        return []
    thr = max(floor + 0.35 * (top - floor), mid)
    voiced = [d > thr for d in db]
    # fill gaps < 200 ms, then drop runs < 100 ms
    gap = 0
    for i, v in enumerate(voiced):
        if v:
            if 0 < gap <= 10 and i - gap - 1 >= 0 and voiced[i - gap - 1]:
                for j in range(i - gap, i):
                    voiced[j] = True
            gap = 0
        else:
            gap += 1
    out, i = [], 0
    while i < n:
        if voiced[i]:
            j = i
            while j < n and voiced[j]:
                j += 1
            if j - i >= 5:
                out.append((start + i * FRAME / RATE, start + j * FRAME / RATE))
            i = j
        else:
            i += 1
    return out


def _parse(content, fmt):
    import pysubs2
    text = content.decode('utf-8', errors='replace') if isinstance(content, bytes) else content
    subs = pysubs2.SSAFile.from_string(text, format_=fmt if fmt in ('srt', 'ass', 'ssa', 'vtt') else None)
    return subs


def _lines(subs):
    out = []
    for ev in subs.events:
        if getattr(ev, 'is_comment', False) or ev.end <= ev.start:
            continue
        if not (ev.plaintext or '').strip():
            continue
        out.append((ev.start / 1000.0, ev.end / 1000.0))
    return out


def _add_trapezoid(d2, a, b, c, d, weight, lo, n):
    """Overlap of [a+o, b+o] with [c, d] as a function of offset o is a
    trapezoid; add weight * it to the second-difference array d2."""
    m = min(b - a, d - c)
    if m <= 0:
        return
    pts = ((c - b, 1.0), (c - b + m, -1.0), (d - a - m, -1.0), (d - a, 1.0))
    for o, s in pts:
        k = int(round((o - lo) / STEP))
        if k < 0:
            k = 0
        if k >= n:
            continue
        d2[k] += s * weight


def _score_curve(lines, windows, ratio, max_offset):
    """score[o] = speech overlap - p_w * window overlap, summed over lines
    and windows, for offsets o in [-max_offset, max_offset]."""
    lo = -max_offset
    n = int(round(2 * max_offset / STEP)) + 1
    d2 = [0.0] * (n + 2)
    for (w0, w1, speech) in windows:
        wlen = w1 - w0
        p = sum(e - s for s, e in speech) / wlen if wlen > 0 else 0.0
        for (s0, s1) in lines:
            a, b = s0 * ratio, s1 * ratio
            if b + max_offset < w0 or a - max_offset > w1:
                continue
            for (c, d) in speech:
                if b + max_offset < c or a - max_offset > d:
                    continue
                _add_trapezoid(d2, a, b, c, d, 1.0, lo, n)
            _add_trapezoid(d2, a, b, w0, w1, -p, lo, n)
    slope, val, out = 0.0, 0.0, [0.0] * n
    for k in range(n):
        slope += d2[k]
        val += slope * STEP
        out[k] = val
    return out


def sync_subtitle(video_path, content, fmt='srt', max_offset=60.0, windows=6,
                  window_len=30.0, timeout=45):
    """Returns (new_content_bytes_or_None, info). info['status'] is one of
    'synced', 'in_sync', 'unsure', 'failed'."""
    info = {'status': 'failed', 'offset': 0.0, 'ratio': 1.0}
    if not shutil.which('ffmpeg'):
        info['reason'] = 'ffmpeg not installed'
        return None, info
    try:
        subs = _parse(content, fmt)
    except Exception as e:
        info['reason'] = f'unreadable subtitle: {e}'
        return None, info
    lines = _lines(subs)
    if len(lines) < 20:
        info['reason'] = 'too few subtitle lines'
        return None, info
    duration = _duration(video_path)
    if not duration or duration < 300:
        info['reason'] = 'unknown or short duration'
        return None, info

    win = []
    for i in range(windows):
        pos = 0.1 + 0.8 * i / max(1, windows - 1)
        start = max(0.0, pos * duration - window_len / 2)
        samples = _read_window(video_path, start, window_len, timeout)
        if samples is None:
            continue
        speech = _speech_intervals(samples, start)
        if speech:
            win.append((start, start + len(samples) / RATE, speech))
    info['windows'] = len(win)
    if len(win) < max(3, windows // 2):
        info['reason'] = f'only {len(win)} usable audio windows'
        return None, info

    sub_in_windows = 0.0
    for (w0, w1, _) in win:
        for (s0, s1) in lines:
            sub_in_windows += max(0.0, min(s1, w1) - max(s0, w0))
    norm = max(sub_in_windows, 1.0)

    best = None          # (score, ratio, offset_index, curve)
    base = None
    for r in RATIOS:
        curve = _score_curve(lines, win, r, max_offset)
        k = max(range(len(curve)), key=curve.__getitem__)
        if r == 1.0:
            base = curve[int(round(max_offset / STEP))]
        if best is None or curve[k] > best[0]:
            best = (curve[k], r, k, curve)
    score, ratio, k, curve = best
    offset = -max_offset + k * STEP

    mean = sum(curve) / len(curve)
    std = math.sqrt(sum((x - mean) ** 2 for x in curve) / len(curve)) or 1e-9
    away = [x for i, x in enumerate(curve) if abs(i - k) * STEP > 1.5]
    second = max(away) if away else mean
    z = (score - mean) / std
    info.update({'offset': round(offset, 2), 'ratio': round(ratio, 5), 'confidence': round(z, 1),
                 'before': round((base or 0) / norm, 3), 'after': round(score / norm, 3),
                 'peak_margin': round((score - second) / norm, 3)})

    if ratio == 1.0 and abs(offset) <= 0.12:
        info['status'] = 'in_sync'
        return None, info
    improvement = (score - (base or 0)) / norm
    if z < 5 or (score - second) / norm < 0.03 or improvement < 0.04:
        info['status'] = 'unsure'
        info['reason'] = 'no clearly better timing found'
        return None, info

    for ev in subs.events:
        ev.start = max(0, int(round((ev.start / 1000.0 * ratio + offset) * 1000)))
        ev.end = max(ev.start + 1, int(round((ev.end / 1000.0 * ratio + offset) * 1000)))
    out_fmt = fmt if fmt in ('srt', 'ass', 'ssa', 'vtt') else 'srt'
    info['status'] = 'synced'
    return subs.to_string(out_fmt).encode('utf-8'), info


def describe(info):
    s = info.get('status')
    if s == 'synced':
        r = info.get('ratio', 1.0)
        fps = '' if r == 1.0 else f", frame rate x{r:.4f}"
        return f"synced: shifted {info['offset']:+.2f}s{fps} (fit {info['before']:.2f} → {info['after']:.2f})"
    if s == 'in_sync':
        return 'already in sync'
    if s == 'unsure':
        return 'left as is: no clearly better timing found'
    return f"not synced: {info.get('reason', 'failed')}"


if __name__ == '__main__':  # python -m utilities.subtitle_sync video.mkv subs.srt
    logging.basicConfig(level=logging.INFO)
    with open(sys.argv[2], 'rb') as f:
        data = f.read()
    new, inf = sync_subtitle(sys.argv[1], data, os.path.splitext(sys.argv[2])[1][1:].lower())
    print(describe(inf), inf)
    if new and len(sys.argv) > 3:
        with open(sys.argv[3], 'wb') as f:
            f.write(new)
