"""End-to-end simulation of magnet manual-assign for anime season packs.

Mirrors prepare_manual_assignment()'s Phase 1 (S/E), Phase 1b (absolute) and
Phase 2 (fuzzy title) against a Judas-style Gintama pack: seasonal SxxExx
files with absolute numbers in parentheses, plus NCED/NCOP extras that used
to leave episodes 1-30 unassigned and then mis-assign leftovers.
"""
import importlib.util
import os
import re
import sys
import unittest
from unittest import mock

try:
    from fuzzywuzzy import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seasons(n_eps):
    return {1: {'episode_count': n_eps,
                'episodes': {e: {'title': f'Episode {e}', 'absolute': e}
                             for e in range(1, n_eps + 1)}}}


def _items(n_eps, title='Gintama'):
    return [{'type': 'episode', 'season_number': 1, 'episode_number': e,
             'item_key': f'ep_s01e{e:02d}', 'episode_title': f'{title} Episode {e}',
             'assigned': False, 'suggested_file_path': None}
            for e in range(1, n_eps + 1)]


def _file(path, seasons=(), episodes=()):
    name = os.path.basename(path)
    return {'original': {'filename': name, 'path': path},
            'parsed': {'seasons': list(seasons), 'episodes': list(episodes)},
            'used': False, 'explicit_absolute': []}


def _judas_pack():
    """Compact Judas-like set: S01E01-35 with NCED/NCOP 1-30, S02+(050), S10E19(360)."""
    files = []
    for n in range(1, 31):
        files.append(_file(f'Extras/[Judas] Gintama - NCED {n:02d}.mkv', episodes=[n]))
        files.append(_file(f'Extras/[Judas] Gintama - NCOP {n:02d}.mkv', episodes=[n]))
    files.append(_file('[Judas] Gintama S01/[Judas] Gintama - S01E01-E02 (001-002).mkv',
                       seasons=[1], episodes=[1, 2]))
    for n in range(3, 36):
        files.append(_file(f'[Judas] Gintama S01/[Judas] Gintama - S01E{n:02d} ({n:03d}).mkv',
                           seasons=[1], episodes=[n]))
    files.append(_file('[Judas] Gintama S02/[Judas] Gintama - S02E01 (050).mkv',
                       seasons=[2], episodes=[1]))
    files.append(_file('[Judas] Gintama S10/[Judas] Gintama - S10E19 (360).mkv',
                       seasons=[10], episodes=[19]))
    return files


def _phase1_se(items, files, show_abs, m):
    """Phase 1 S/E matching from magnet_routes (extras already marked used)."""
    assigned = 0
    for item in items:
        if item['assigned']:
            continue
        s, e = item['season_number'], item['episode_number']
        item_abs = show_abs.get((s, e))
        matches = []
        for f in files:
            if f['used']:
                continue
            if m.explicit_absolute_conflicts(item_abs, f.get('explicit_absolute')):
                continue
            parsed = f['parsed']
            seasons, episodes = parsed.get('seasons') or [], parsed.get('episodes') or []
            season_match = (s in seasons) or (not seasons and s == 1)
            if season_match and e in episodes:
                matches.append(f)
        if len(matches) == 1:
            item['suggested_file_path'] = matches[0]['original']['filename']
            item['assigned'] = True
            matches[0]['used'] = True
            assigned += 1
    return assigned


def _phase2_title(items, files, series_title, m, threshold=85):
    """Phase 2 fuzzy title matching from magnet_routes (with series-title guard)."""
    if fuzz is None:
        raise unittest.SkipTest('fuzzywuzzy not installed')
    assigned = 0
    unassigned = [i for i in items if not i['assigned'] and i['type'] == 'episode']
    for f in [x for x in files if not x['used']]:
        raw = f['original'].get('filename') or ''
        base = os.path.splitext(raw)[0].lower()
        cleaned = re.sub(r'[\(\[].*?[\)\]]', '', base)
        cleaned = re.sub(r'\b(s(\d{1,2})e(\d{1,3})|season\s*\d{1,2}\s*episode\s*\d{1,3})\b',
                         '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'[\s._-]+$', '', cleaned).strip()
        if m.phase2_title_too_weak(cleaned, series_title):
            continue
        best, best_score, best_idx = None, 0, -1
        for idx, item in enumerate(unassigned):
            title = (item.get('episode_title') or '').lower()
            if not title or title == f'episode {item.get("episode_number")}':
                continue
            score = fuzz.token_set_ratio(title, cleaned)
            if score > best_score and score >= threshold:
                best, best_score, best_idx = item, score, idx
        if best is not None:
            best['suggested_file_path'] = f['original']['filename']
            best['assigned'] = True
            f['used'] = True
            assigned += 1
            del unassigned[best_idx]
    return assigned


class MagnetAssignAnimePackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        anime_utils = _load('anime_utils_magnet_sim', 'scraper/functions/anime_utils.py')
        patcher = mock.patch.dict(sys.modules, {'scraper.functions.anime_utils': anime_utils})
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.m = _load('manual_assign_magnet_sim', 'utilities/manual_assign_episodes.py')

    def test_extra_tag_detection(self):
        yes = [
            '[Judas] Gintama - NCED 01.mkv',
            '[Judas] Gintama - NCOP 16a.mkv',
            'Show - NCOP1.mkv',
            'Show - OP01.mkv',
            'Show - OP 03.mkv',
            'Show - ED02.mkv',
            'Show - Opening 1.mkv',
            'Show - Menu01.mkv',
            'Show - Menu 02.mkv',
            'Show - PV01.mkv',
            'Show - PV 02.mkv',
            'Show - CM01.mkv',
            'Show - CleanOP 01.mkv',
            'Show - CleanED01.mkv',
        ]
        no = [
            '[Judas] Gintama - S01E03 (003).mkv',
            '[Anime Time] Gintama - 001.mkv',
            'Show - OVA 01.mkv',
            'Show Special - The Semi-Final - 01.mkv',
            'Show - Episode 01.mkv',
        ]
        for name in yes:
            self.assertTrue(self.m.is_anime_op_ed_extra(name), name)
        for name in no:
            self.assertFalse(self.m.is_anime_op_ed_extra(name), name)

    def test_judas_gintama_simulation(self):
        """Full assign pass: NCED/NCOP no longer block 1-30; (360) and (050) map by absolute."""
        n_eps = 370
        seasons = _seasons(n_eps)
        items = _items(n_eps)
        files = _judas_pack()
        show_abs = self.m.absolute_numbers(seasons)

        for f in files:
            if self.m.is_anime_op_ed_extra(f['original']['filename']) or \
               self.m.is_anime_op_ed_extra(f['original']['path']):
                f['used'] = True
            f['explicit_absolute'] = self.m.file_explicit_absolute(
                f['parsed'], f['original']['filename'], seasons)

        # Sanity: extras skipped, real episode files still free
        skipped = sum(1 for f in files if f['used'])
        self.assertEqual(skipped, 60)  # NCED 1-30 + NCOP 1-30
        self.assertFalse(any(f['used'] and 'S01E' in f['original']['path'] for f in files))

        p1 = _phase1_se(items, files, show_abs, self.m)
        p1b = self.m.match_absolute_files(items, files, seasons)
        p2 = _phase2_title(items, files, 'Gintama', self.m)

        by_ep = {i['episode_number']: i for i in items}

        # Early eps: real S01 files, not NCED/NCOP
        self.assertTrue(by_ep[1]['assigned'])
        self.assertIn('S01E01-E02', by_ep[1]['suggested_file_path'])
        self.assertTrue(by_ep[2]['assigned'])
        self.assertIn('S01E01-E02', by_ep[2]['suggested_file_path'])
        self.assertTrue(by_ep[3]['assigned'])
        self.assertIn('S01E03 (003)', by_ep[3]['suggested_file_path'])
        self.assertTrue(by_ep[30]['assigned'])
        self.assertIn('S01E30 (030)', by_ep[30]['suggested_file_path'])

        # Later S01 still by S/E; S02+(050) and S10E19(360) by absolute
        self.assertIn('S01E35 (035)', by_ep[35]['suggested_file_path'])
        self.assertIn('S02E01 (050)', by_ep[50]['suggested_file_path'])
        self.assertIn('S10E19 (360)', by_ep[360]['suggested_file_path'])

        # Phase 2 must not stick a leftover S01 file onto a late Gintama-titled slot
        for e in (368, 369, 370):
            path = by_ep[e].get('suggested_file_path') or ''
            self.assertNotIn('S01E', path, f'ep {e} wrongly got {path}')

        # Extras never suggested
        for item in items:
            path = item.get('suggested_file_path') or ''
            self.assertNotIn('NCED', path)
            self.assertNotIn('NCOP', path)

        self.assertGreaterEqual(p1 + p1b, 35)
        self.assertEqual(p2, 0)  # nothing weak enough left for title-only matching


if __name__ == '__main__':
    unittest.main()
