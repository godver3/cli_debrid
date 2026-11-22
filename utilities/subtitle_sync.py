"""Subtitle timing sync from sampled windows of the video.

Fits a subtitle to the video with one offset and an optional frame-rate
ratio (23.976 <-> 24 <-> 25 fps releases), the way alass does without
splits. Unlike alass/ffsubsync it never reads the whole file: a handful of
short windows spread through it are read (input seeking, so on a
debrid/usenet mount only those parts are fetched) and the subtitle lines are
matched against a reference from those windows:

  1. an embedded text subtitle track (SRT/ASS/mov_text) when the file has
     one: its line timings are exact for this file, and ffprobe only lists
     packet timestamps, nothing is decoded;
  2. otherwise speech detected in the audio.

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
import time

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
                           capture_output=True, text=True, timeout=60)
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


EMBED_TEXT_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}


def _embedded_text_stream(path, timeout=20):
    """(position among subtitle streams, language) of the first non-forced
    embedded text subtitle track, or None. Header read only."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's',
                            '-show_entries', 'stream=codec_name:stream_tags=language:stream_disposition=forced',
                            '-of', 'json', str(path)], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        import json
        streams = json.loads(r.stdout or '{}').get('streams') or []
    except Exception:
        return None
    for pos, st in enumerate(streams):
        if (st.get('disposition') or {}).get('forced'):
            continue
        if (st.get('codec_name') or '').lower() in EMBED_TEXT_CODECS:
            return pos, ((st.get('tags') or {}).get('language') or 'und')
    return None


def _read_sub_window(path, pos, start, length, timeout):
    """Line intervals (absolute seconds) of embedded subtitle stream `pos` in
    [start, start+length), from packet timestamps, or None on failure."""
    cmd = ['ffprobe', '-v', 'error', '-read_intervals', f'{start:.3f}%+{length:.3f}',
           '-select_streams', f's:{pos}', '-show_entries', 'packet=pts_time,duration_time,size',
           '-of', 'csv=p=0', str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.info(f"[subsync] subtitle read at {start:.0f}s timed out after {timeout}s")
        return None
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = []
    end = start + length
    for line in r.stdout.splitlines():
        parts = line.strip().split(',')
        if len(parts) < 2:
            continue
        try:
            pts, dur = float(parts[0]), float(parts[1])
            size = int(parts[2]) if len(parts) > 2 and parts[2] else 99
        except ValueError:
            continue
        if dur <= 0.05 or size <= 2:        # mov_text "clear" samples
            continue
        a, b = max(pts, start), min(pts + dur, end)
        if b > a:
            out.append((a, b))
    return out


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
    floor, top = srt[n // 10], srt[int(n * 0.95)]
    if top - floor < 6:          # flat audio: silence or constant noise
        return []
    # Threshold from the noise floor up. (Clamping it to the median forced
    # about half of every window to count as speech whatever was said, so
    # the detector couldn't follow dialogue density at all.)
    thr = floor + 0.4 * (top - floor)
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


# Windows per reference. Every window is a jump in the file: on a debrid/
# usenet mount that is a run of article fetches plus the mount's read-ahead,
# and the line speed is the limit, so fewer and shorter windows are what make
# a sync fast. Embedded subtitle lines are exact, so short windows suffice;
# speech detection needs more audio to be sure.
EMBED_WINDOWS, EMBED_LEN = 5, 30.0
EMBED_TIMEOUT = 45
AUDIO_WINDOWS, AUDIO_LEN = 5, 20.0


def _starts(duration, n, length):
    return [max(0.0, (0.1 + 0.8 * i / max(1, n - 1)) * duration - length / 2) for i in range(n)]


def sync_subtitle(video_path, content, fmt='srt', max_offset=60.0, timeout=90, duration=None,
                  duration_fallback=None, audio=False):
    """Returns (new_content_bytes_or_None, info). info['status'] is one of
    'synced', 'in_sync', 'unsure', 'failed'. `duration` (exact seconds, e.g.
    from Plex) skips probing the file; `duration_fallback` (approximate, e.g.
    the metadata runtime) is used only when probing fails."""
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
    if not duration or duration < 300:
        duration = _duration(video_path)
    if (not duration or duration < 300) and duration_fallback and duration_fallback >= 300:
        duration = float(duration_fallback) * 0.97   # keep the last window inside the file
    if not duration or duration < 300:
        info['reason'] = 'video length unknown (the file could not be probed in time)'
        return None, info

    # Windows are read one at a time (parallel reads share the same line and
    # mount and only time out more), and reading stops as soon as the fit is
    # clear, usually after 3 windows.
    t_read = time.time()
    win, result = [], None
    ref = _embedded_text_stream(video_path, timeout=timeout)
    if ref is not None:
        pos, lang = ref
        info['reference'] = f'embedded subtitles ({lang})'
        for start in _starts(duration, EMBED_WINDOWS, EMBED_LEN):
            t_w = time.time()
            lines_w = _read_sub_window(video_path, pos, start, EMBED_LEN, EMBED_TIMEOUT)
            if lines_w is None and time.time() - t_w >= EMBED_TIMEOUT - 1:
                # a cold file on the mount that can't serve one window in time
                # won't serve five: stop instead of tying up the queue
                info['status'] = 'slow'
                info['reason'] = f'reading the file took over {EMBED_TIMEOUT}s per sample'
                info['read_s'] = round(time.time() - t_read, 1)
                return None, info
            if lines_w is not None:
                win.append((start, start + EMBED_LEN, lines_w))
            if sum(1 for w in win if w[2]) >= 3:
                result = _match_lines(lines, win, max_offset)
                if result['decided']:
                    break
        if sum(1 for w in win if w[2]) < 2 or sum(len(w[2]) for w in win) < 10:
            win, result = [], None
        elif result is None or not result['decided']:
            result = _match_lines(lines, win, max_offset)
            if result['status'] == 'unsure':
                # the embedded track doesn't agree with this subtitle (e.g. a
                # partial or foreign track): fall back to the audio
                info['embedded_note'] = result['info'].get('agree')
                win, result = [], None
    if not win and not audio:
        # Speech detection on short audio samples isn't reliable on real TV
        # audio (measured: it rarely decides, and decoding the whole track
        # means reading the whole file from the mount), so without an
        # embedded text track the subtitle is left as it is.
        info['status'] = 'no_reference'
        info['reason'] = 'no embedded text subtitle in the file to check the timing against'
        info['read_s'] = round(time.time() - t_read, 1)
        return None, info
    if not win:
        info['reference'] = 'audio'
        for start in _starts(duration, AUDIO_WINDOWS, AUDIO_LEN):
            samples = _read_window(video_path, start, AUDIO_LEN, timeout)
            if samples is not None:
                speech = _speech_intervals(samples, start)
                if speech:
                    win.append((start, start + len(samples) / RATE, speech))
            if len(win) >= 3:
                result = _evaluate(lines, win, max_offset)
                if result['decided']:
                    break
    info['read_s'] = round(time.time() - t_read, 1)
    info['windows'] = len(win)
    if len(win) < 3 and info['reference'] == 'audio':
        info['reason'] = f'only {len(win)} of {AUDIO_WINDOWS} audio samples could be read from the file in time'
        return None, info
    if result is None:
        result = _evaluate(lines, win, max_offset)
    info.update(result['info'])
    info['status'] = result['status']
    if result['status'] != 'synced':
        if result['status'] == 'unsure':
            info['reason'] = 'no clearly better timing found'
        return None, info
    ratio, offset = result['ratio'], result['offset']
    for ev in subs.events:
        ev.start = max(0, int(round((ev.start / 1000.0 * ratio + offset) * 1000)))
        ev.end = max(ev.start + 1, int(round((ev.end / 1000.0 * ratio + offset) * 1000)))
    out_fmt = fmt if fmt in ('srt', 'ass', 'ssa', 'vtt') else 'srt'
    return subs.to_string(out_fmt).encode('utf-8'), info


MATCH_TOL = 0.5      # a line counts as matching when its start is within this of a reference line
MATCH_SHARE = 0.6    # ... and at least this share of lines must match


def _match_lines(lines, win, max_offset):
    """Fit against an exact reference (embedded subtitle lines): every pair
    of (subtitle line, reference line) votes for an offset; the most-voted
    offset (per frame-rate ratio) is refined to the median of its votes, and
    the answer counts only when at least half of the subtitle lines then
    start within MATCH_TOL of a reference line."""
    refs = sorted(a for (_, _, w) in win for (a, _) in w)
    spans = [(w0, w1) for (w0, w1, _) in win]
    if not refs:
        return {'status': 'unsure', 'decided': False, 'ratio': 1.0, 'offset': 0.0, 'info': {'agree': 0.0}}
    import bisect
    best = None
    for r in RATIOS:
        starts = [a * r for (a, _) in lines]
        votes = {}
        for x in starts:
            lo = bisect.bisect_left(refs, x - max_offset)
            hi = bisect.bisect_right(refs, x + max_offset)
            for y in refs[lo:hi]:
                b = round((y - x) / 0.1)
                votes[b] = votes.get(b, 0) + 1
        if not votes:
            continue
        peak = max(votes, key=lambda b: votes[b] + 0.5 * (votes.get(b - 1, 0) + votes.get(b + 1, 0)))
        est = peak * 0.1
        # refine, then measure agreement on lines that land inside the windows
        deltas, considered, agree = [], 0, 0
        per_win = {}
        for x in starts:
            xs = x + est
            w = next((k for k, (w0, w1) in enumerate(spans) if w0 <= xs <= w1), None)
            if w is None:
                continue
            considered += 1
            i = bisect.bisect_left(refs, xs)
            near = min((refs[j] for j in (i - 1, i) if 0 <= j < len(refs)), key=lambda y: abs(y - xs))
            if abs(near - xs) <= 1.0:
                deltas.append((near - x, w))
        if deltas:
            est = sorted(d for d, _ in deltas)[len(deltas) // 2]
            for d, w in deltas:
                tot = per_win.setdefault(w, [0, 0])
                tot[1] += 1
                if abs(d - est) <= MATCH_TOL:
                    agree += 1
                    tot[0] += 1
        share = agree / considered if considered else 0.0
        # the same offset must hold in separate parts of the file, not just
        # overall (dense lines can line up by chance in one stretch)
        windows_ok = sum(1 for a, t in per_win.values() if t >= 4 and a / t >= MATCH_SHARE)
        if windows_ok < 2:
            share = min(share, MATCH_SHARE - 0.01)
        if best is None or (share, -abs(r - 1)) > (best[0], -abs(best[1] - 1)):
            best = (share, r, est, considered)
    if best is None:
        return {'status': 'unsure', 'decided': False, 'ratio': 1.0, 'offset': 0.0, 'info': {'agree': 0.0}}
    share, ratio, offset, considered = best
    info = {'offset': round(offset, 2), 'ratio': round(ratio, 5), 'confidence': round(share * 10, 1),
            'agree': round(share, 2), 'lines_checked': considered}
    if considered < 8 or share < MATCH_SHARE:
        status = 'unsure'
    elif ratio == 1.0 and abs(offset) <= 0.15:
        status = 'in_sync'
    else:
        status = 'synced'
    return {'status': status, 'ratio': ratio, 'offset': offset, 'info': info,
            'decided': status != 'unsure' and considered >= 20}


def _evaluate(lines, win, max_offset):
    """Best (ratio, offset) for the windows read so far. 'decided' when the
    answer is already clear (in sync, or a confident fix), so reading more
    windows wouldn't change it."""
    sub_in_windows = 0.0
    for (w0, w1, _) in win:
        for (s0, s1) in lines:
            sub_in_windows += max(0.0, min(s1, w1) - max(s0, w0))
    norm = max(sub_in_windows, 1.0)
    best, base = None, None
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
    margin = (score - second) / norm
    improvement = (score - (base or 0)) / norm
    info = {'offset': round(offset, 2), 'ratio': round(ratio, 5), 'confidence': round(z, 1),
            'before': round((base or 0) / norm, 3), 'after': round(score / norm, 3),
            'peak_margin': round(margin, 3)}
    if ratio == 1.0 and abs(offset) <= 0.12:
        status = 'in_sync'
    elif z < 5 or margin < 0.03 or improvement < 0.04:
        status = 'unsure'
    else:
        status = 'synced'
    decided = z >= 6 and margin >= 0.05 and status != 'unsure'
    return {'status': status, 'ratio': ratio, 'offset': offset, 'info': info, 'decided': decided}


def describe(info):
    s = info.get('status')
    if s == 'synced':
        r = info.get('ratio', 1.0)
        fps = '' if r == 1.0 else f", frame rate x{r:.4f}"
        ref = f" against {info['reference']}" if info.get('reference') else ''
        return f"synced{ref}: shifted {info['offset']:+.2f}s{fps} (fit {info['before']:.2f} → {info['after']:.2f})"
    ref = f" (checked against {info['reference']})" if info.get('reference') else ''
    if s == 'in_sync':
        return 'already in sync' + ref
    if s == 'unsure':
        return 'left as is: no clearly better timing found' + ref
    if s == 'slow':
        return f"not checked: {info.get('reason', 'the file was too slow to read')}"
    if s == 'no_reference':
        return 'not checked: no embedded text subtitle in the file to compare with'
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
