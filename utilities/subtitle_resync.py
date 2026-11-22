"""Resync subtitles that already exist: sidecar files and Plex external/
uploaded subtitles. Used by the per-subtitle Sync button and the "Sync
existing subtitles" bulk run.

Each checked subtitle's content hash is remembered (subtitle_history.
sync_checked), so a bulk run skips subtitles it already synced or found in
sync, including the new copy it just uploaded to Plex.
"""
import hashlib
import logging
import os

import requests

PLEX_HEADERS = {
    'X-Plex-Client-Identifier': 'cli-debrid-subtitles',
    'X-Plex-Product': 'cli_debrid',
    'X-Plex-Version': '1.0',
}


def plex_conn():
    from utilities.settings import get_setting
    return (get_setting('Plex', 'url', '') or '').rstrip('/'), get_setting('Plex', 'token', '') or ''


_INFO_CACHE = {}   # rating_key -> (monotonic, streams, duration_s)


def plex_item_info(rating_key, max_age=20):
    """(subtitle streams or None, duration seconds or None) from one Plex
    metadata request, briefly cached so a resync's stream lookup and its
    duration hint share it."""
    import time as _t
    hit = _INFO_CACHE.get(str(rating_key))
    if hit and _t.monotonic() - hit[0] < max_age:
        return hit[1], hit[2]
    streams, duration = _fetch_item_info(rating_key)
    if len(_INFO_CACHE) > 500:
        _INFO_CACHE.clear()
    _INFO_CACHE[str(rating_key)] = (_t.monotonic(), streams, duration)
    return streams, duration


def plex_streams(rating_key):
    """Subtitle streams Plex has for an item (embedded and external), or None."""
    return plex_item_info(rating_key, max_age=0)[0]


def _fetch_item_info(rating_key):
    url, token = plex_conn()
    if not (url and token and rating_key):
        return None, None
    try:
        r = requests.get(f"{url}/library/metadata/{rating_key}", params={'X-Plex-Token': token},
                         headers={'Accept': 'application/json'}, timeout=10)
        if r.status_code != 200:
            return None, None
        meta = (r.json().get('MediaContainer') or {}).get('Metadata') or []
        out = []
        duration = None
        for m in meta[:1]:
            try:
                duration = float(m.get('duration') or 0) / 1000.0 or None
            except (TypeError, ValueError):
                duration = None
            for media in m.get('Media') or []:
                for part in media.get('Part') or []:
                    for st in part.get('Stream') or []:
                        if st.get('streamType') != 3:
                            continue
                        out.append({
                            'id': st.get('id'),
                            'key': st.get('key') or '',
                            'language': st.get('languageTag') or st.get('languageCode') or '',
                            'codec': st.get('codec') or '',
                            'title': st.get('displayTitle') or st.get('extendedDisplayTitle') or '',
                            'forced': bool(st.get('forced')),
                            'external': bool(st.get('key')),
                        })
        return out, duration
    except Exception as e:
        logging.debug(f"[subs] Plex stream lookup failed for {rating_key}: {e}")
        return None, None


def plex_stream_of_item(item, stream_id):
    """The external subtitle stream `stream_id` of this item, or None."""
    for st in plex_streams(item.get('ms_item_id')) or []:
        if str(st.get('id')) == str(stream_id) and st.get('external') and st.get('key'):
            return st
    return None


def plex_delete(st):
    url, token = plex_conn()
    r = requests.delete(url + st['key'], params={'X-Plex-Token': token}, headers=PLEX_HEADERS, timeout=30)
    return r.status_code in (200, 204), r.status_code


def _digest(content):
    return hashlib.sha1(content).hexdigest()


HASH_PENDING = 'left as is'      # sync couldn't decide and no hash search yet


def _checked(content):
    """Fully handled: checked, and not an undecided sync still owed a
    hash-match search."""
    from database import subtitle_history
    res = subtitle_history.sync_result(_digest(content))
    return res is not None and not _hash_pending(res)


def _hash_pending(result):
    return result.startswith(HASH_PENDING) and 'no hash match' not in result


def _mark(content, result):
    from database import subtitle_history
    subtitle_history.mark_sync_checked(_digest(content), result)


def resync_sidecar(item, path, skip_checked=False):
    """Returns {'changed', 'message', 'skipped'}."""
    from utilities import downsub
    video = downsub.resolve_item_path(item)
    if not video:
        return {'changed': False, 'message': 'no file path for this item', 'error': True}
    real = os.path.realpath(video) if os.path.islink(video) else video
    with open(path, 'rb') as f:
        content = f.read()
    if skip_checked and _checked(content):
        return {'changed': False, 'skipped': True, 'message': 'checked before'}
    from utilities.subtitle_status import label
    new, note = downsub.sync_content(real, content, os.path.splitext(path)[1][1:].lower(),
                                     label=f"{label(item)} {os.path.basename(path)}", item=item)
    if new != content:
        tmp = path + '.syncing'
        with open(tmp, 'wb') as f:
            f.write(new)
        os.replace(tmp, path)
        _mark(new, 'synced')
        return {'changed': True, 'message': note}
    if note and note.startswith(('already in sync', 'left as is', 'not checked: no embedded')):
        _mark(content, note)
    if note and note.startswith('not checked:') and not note.startswith('not checked: no embedded'):
        note = 'kept: no subtitle for this release found; ' + note[len('not checked: '):]
    elif note and note.startswith('not checked: no embedded'):
        note = 'kept: no subtitle for this release found, and no embedded subtitles to check the timing'
    return {'changed': False, 'message': note or 'not synced'}


def resync_plex_stream(item, st, skip_checked=False):
    """Download an external Plex subtitle, sync it, upload the fixed copy and
    remove the old one. Returns {'changed', 'message', 'skipped', 'error'}."""
    from utilities import downsub
    url, token = plex_conn()
    video = downsub.resolve_item_path(item)
    if not video:
        return {'changed': False, 'message': 'no file path for this item', 'error': True}
    r = requests.get(url + st['key'], params={'X-Plex-Token': token}, headers=PLEX_HEADERS, timeout=30)
    if r.status_code != 200 or not r.content:
        return {'changed': False, 'message': f'could not download it from Plex (HTTP {r.status_code})', 'error': True}
    content = r.content
    if skip_checked and _checked(content):
        return {'changed': False, 'skipped': True, 'message': 'checked before'}
    fmt = (st.get('codec') or 'srt').lower()
    from utilities.subtitle_status import label
    new, note = downsub.sync_content(video, content, fmt,
                                     label=f"{label(item)} {st.get('language') or 'und'} (Plex)", item=item)
    if new == content:
        if note and note.startswith(('already in sync', 'left as is', 'not checked: no embedded')):
            _mark(content, note)
        return {'changed': False, 'message': note or 'not synced'}
    if not downsub.upload_subtitle_to_plex(item.get('ms_item_id'), new, st.get('language') or 'und', fmt=fmt):
        return {'changed': False, 'message': 'synced, but the upload to Plex failed; the old subtitle was kept',
                'error': True}
    _mark(new, 'synced')
    ok, code = plex_delete(st)
    if not ok:
        return {'changed': True, 'message': f'{note}; uploaded, but Plex would not remove the old one (HTTP {code})'}
    return {'changed': True, 'message': note}


def _lang_of(code):
    from babelfish import Language
    c = (code or '').strip()
    for parse in (Language.fromietf, Language.fromalpha2, Language):
        try:
            return parse(c)
        except Exception:
            continue
    return None


class _HashFinder:
    """Per item: the file hash and OpenSubtitles hash matches, looked up once
    and only when a subtitle of the item actually needs checking."""

    def __init__(self, item):
        self.item = item
        self._ctx = False
        self._found = {}

    def ctx(self):
        if self._ctx is False:
            from utilities import downsub
            self._ctx = None
            try:
                path = downsub.resolve_item_path(self.item)
                if path:
                    self._ctx = downsub.prepare_video(path, item=self.item, check_embedded=False)
            except Exception as e:
                logging.debug(f"[subs] hash lookup prepare failed: {e}")
        return self._ctx

    def searched(self):
        """True once a hash search actually ran for this item."""
        return bool(self._found)

    def best(self, lang_code, hi=False):
        """A downloaded subtitle for this language made for this file: a hash
        match first, else one for the same release (group + source). Returns
        (sub, fmt, ext, kind) or None."""
        from utilities import downsub
        lang = _lang_of(lang_code)
        ctx = self.ctx()
        if lang is None or not ctx or ctx.get('all_embedded') or not ctx.get('video'):
            return None
        key = str(lang)
        if key not in self._found:
            found = []
            try:
                found += [(s, 'hash match') for s in downsub.hash_matches(ctx, [lang])]
            except Exception as e:
                logging.debug(f"[subs] hash search failed: {e}")
            try:
                seen = {id(s) for s, _ in found}
                found += [(s, k) for s, k in downsub.release_matches(ctx, [lang]) if id(s) not in seen]
            except Exception as e:
                logging.debug(f"[subs] release search failed: {e}")
            self._found[key] = found
        cands = sorted(self._found[key],
                       key=lambda x: (x[1] != 'hash match', bool(getattr(x[0], 'hearing_impaired', False)) != hi))
        for sub, kind in cands:
            if downsub.fetch_subtitle(sub):
                fmt, ext = downsub._final_format(sub)
                if fmt:
                    return sub, fmt, ext, kind
        return None


def _hash_replace_sidecar(item, path, finder):
    """Replace a sidecar with a subtitle made for this exact file. Returns a
    result dict, or None when there's no hash match."""
    from utilities import downsub
    name = os.path.basename(path)
    parts = name.split('.')
    lang = parts[-2] if len(parts) >= 3 else ''
    hi = False
    if lang.lower() in ('hi', 'sdh', 'cc') and len(parts) >= 4:
        hi, lang = True, parts[-3]
    got = finder.best(lang, hi=hi)
    if not got:
        return None
    sub, fmt, ext, kind = got
    content = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
    with open(path, 'rb') as f:
        if f.read() == content:
            _mark(content, kind)
            return {'changed': False, 'message': f'already the subtitle made for this file ({kind})'}
    target = os.path.splitext(path)[0] + ext
    tmp = target + '.syncing'
    with open(tmp, 'wb') as f:
        f.write(content)
    os.replace(tmp, target)
    if target != path:
        try:
            os.remove(path)
        except OSError:
            pass
    _mark(content, kind)
    ctx = finder.ctx()
    downsub._record_history(dict(ctx, _sync_note=f'{kind}, no sync needed'), sub, lang, fmt, 'sidecar',
                            target, kind.replace(' ', '-'))
    return {'changed': True, 'message': f'replaced with a subtitle for this release ({kind}, {sub.provider_name})'}


def _hash_replace_plex(item, st, finder):
    from utilities import downsub
    got = finder.best(st.get('language') or '', hi=False)
    if not got:
        return None
    sub, fmt, _, kind = got
    content = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
    lang3 = getattr(sub.language, 'alpha3', None) or (st.get('language') or 'und')
    if not downsub.upload_subtitle_to_plex(item.get('ms_item_id'), content, lang3, fmt=fmt):
        return None
    _mark(content, kind)
    ok, code = plex_delete(st)
    ctx = finder.ctx()
    downsub._record_history(dict(ctx, _sync_note=f'{kind}, no sync needed'), sub,
                            getattr(sub.language, 'ietf', None) or str(sub.language), fmt, 'plex', None,
                            kind.replace(' ', '-'))
    msg = f'replaced with a subtitle for this release ({kind}, {sub.provider_name})'
    return {'changed': True, 'message': msg if ok else f'{msg}; Plex would not remove the old one (HTTP {code})'}


def resync_item(item, skip_checked=True):
    """Every existing subtitle of one item: sidecar files are fixed in place
    (both modes); in Plex mode, uploaded Plex subtitles for languages without
    a sidecar are fixed through Plex. Returns (changed, checked, notes)."""
    from utilities.subtitle_status import is_plex_mode, sidecar_langs
    changed = checked = 0
    notes = []
    sidecars = sidecar_langs(item.get('location_on_disk'), {})
    finder = _HashFinder(item)
    for lang, paths in sidecars.items():
        for p in paths:
            res = None
            try:
                with open(p, 'rb') as f:
                    content = f.read()
            except OSError:
                continue
            from database import subtitle_history
            prev = subtitle_history.sync_result(_digest(content))
            if skip_checked and prev is not None and not _hash_pending(prev):
                continue
            try:
                res = _hash_replace_sidecar(item, p, finder)
            except Exception as e:
                logging.debug(f"[subs] hash replace failed for {p}: {e}")
            if res is None and prev is not None and _hash_pending(prev):
                # sync already couldn't decide; only the hash search was owed
                from utilities import downsub as _ds
                if any(x in _ds.HASH_PROVIDERS for x in _ds.selected_providers()):
                    _mark(content, prev + '; no hash match')
                res = {'changed': False, 'message': 'kept: no subtitle for this release found (its timing was already checked)'}
            if res is None:
                res = resync_sidecar(item, p, skip_checked=skip_checked)
                if finder.searched() and (res.get('message') or '').startswith(HASH_PENDING):
                    _mark(content, res['message'] + '; no hash match')
            if not res.get('skipped'):
                checked += 1
                notes.append(f"{lang}: {res['message']}")
            changed += 1 if res.get('changed') else 0
    if is_plex_mode():
        have = {k.split('-')[0] for k in sidecars}
        for st in plex_item_info(item.get('ms_item_id'))[0] or []:
            if not (st.get('external') and st.get('key')):
                continue
            if (st.get('language') or 'und').split('-')[0].lower() in have:
                continue          # that's the sidecar file Plex picked up
            res = None
            prev = None
            content = None
            url, token = plex_conn()
            try:
                r = requests.get(url + st['key'], params={'X-Plex-Token': token}, headers=PLEX_HEADERS,
                                 timeout=30)
                if r.status_code == 200 and r.content:
                    content = r.content
            except requests.RequestException:
                pass
            if content is not None:
                from database import subtitle_history
                prev = subtitle_history.sync_result(_digest(content))
                if skip_checked and prev is not None and not _hash_pending(prev):
                    continue
            try:
                res = _hash_replace_plex(item, st, finder)
            except Exception as e:
                logging.debug(f"[subs] hash replace failed for Plex stream {st.get('id')}: {e}")
            if res is None and prev is not None and _hash_pending(prev):
                from utilities import downsub as _ds
                if any(x in _ds.HASH_PROVIDERS for x in _ds.selected_providers()):
                    _mark(content, prev + '; no hash match')
                res = {'changed': False, 'message': 'kept: no subtitle for this release found (its timing was already checked)'}
            if res is None:
                res = resync_plex_stream(item, st, skip_checked=skip_checked)
            if not res.get('skipped'):
                checked += 1
                notes.append(f"{st.get('language') or 'und'} (Plex): {res['message']}")
            changed += 1 if res.get('changed') else 0
    return changed, checked, notes


def needs_check(item, cache=None):
    """True when the item has a subtitle Sync existing hasn't checked yet: an
    unchecked sidecar file, or (Plex mode) a subtitle cli_debrid uploaded to
    Plex. Sidecars are read and hashed locally; Plex isn't asked."""
    from database import subtitle_history
    from utilities.subtitle_status import is_plex_mode, sidecar_langs
    paths = [p for ps in sidecar_langs(item.get('location_on_disk'), cache if cache is not None else {}).values()
             for p in ps]
    for p in paths:
        try:
            with open(p, 'rb') as f:
                if not _checked(f.read()):
                    return True
        except OSError:
            continue
    if is_plex_mode() and item.get('id') is not None:
        langs_with_file = {os.path.basename(p).split('.')[-2].split('-')[0].lower() for p in paths
                           if p.count('.') >= 2}
        uploaded = subtitle_history.languages_by_item(
            [item['id']], {item['id']: subtitle_history.media_key_of(item)}).get(item['id'], set())
        if any((lang or '').split('-')[0].lower() not in langs_with_file for lang in uploaded):
            return True
    return False
