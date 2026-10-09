"""A release tagged with an audio language other than the version's language
code must rank below untagged releases. Repro: How I Met Your Mother S01E18
(language_code 'en') picked '...Der.Anstaendige.GERMAN.WS.HDTV...' over English
WEB-DLs because preferred_language was None for 'en' and the old -30 penalty
(x language_weight 3) was smaller than the size/bitrate gap anyway."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from PTT import parse_title

from scraper.functions.rank_results import rank_result_key

GERMAN = 'How.I.Met.Your.Mother.S01E18.Der.Anstaendige.GERMAN.WS.HDTV.AC3.1080p.x264-ATG'
ENGLISH = 'How.I.Met.Your.Mother.S01E18.1080p.AMZN.WEBRip.DDP5.1.x264-NOGRP'
MULTI = 'How.I.Met.Your.Mother.S01E18.MULTi.GERMAN.1080p.WEB.x264-X'


def _result(title):
    parsed = parse_title(title)
    parsed['resolution_rank'] = 3
    return {'title': title, 'original_title': title, 'parsed_info': parsed,
            'size': 1.2, 'bitrate': 6000}


def _score(title, preferred_language='en', upgrade_mode=False, **settings):
    version = {'max_resolution': '1080p', 'language_code': 'en'}
    version.update(settings)
    result = _result(title)
    rank_result_key(result, [result], 'How I Met Your Mother', 2005, 1, 18, False,
                    'episode', version, preferred_language=preferred_language,
                    upgrade_mode=upgrade_mode)
    return result['score_breakdown']


class ForeignLanguagePenaltyTests(unittest.TestCase):
    def test_default_penalty_is_300_unweighted(self):
        german, english = _score(GERMAN), _score(ENGLISH)
        self.assertEqual(german['foreign_language_penalty'], -300)
        self.assertEqual(english['foreign_language_penalty'], 0)
        # Same size/bitrate inputs, so the gap is the penalty alone.
        self.assertAlmostEqual(english['total_score'] - german['total_score'], 300, delta=1)

    def test_setting_overrides_and_zero_disables(self):
        self.assertEqual(_score(GERMAN, foreign_language_penalty=120)['foreign_language_penalty'], -120)
        self.assertEqual(_score(GERMAN, foreign_language_penalty=0)['foreign_language_penalty'], 0)

    def test_language_weight_does_not_scale_it(self):
        self.assertEqual(_score(GERMAN, language_weight=10)['foreign_language_penalty'], -300)

    def test_multi_audio_exempt(self):
        # PTT returns ['de'] for these (never 'multi'); the name marks dual audio.
        for title in (MULTI,
                      'How.I.Met.Your.Mother.S04E10.Weicheier.German.DD20.Synced.DL.1080p.BD.x264-TVS',
                      'How.I.Met.Your.Mother.S01E18.Dual-Audio.GERMAN.1080p.WEB.x264-X'):
            with self.subTest(title=title):
                self.assertEqual(_score(title)['foreign_language_penalty'], 0)

    def test_dubbed_and_web_dl_not_mistaken_for_multi_audio(self):
        for title in ('How.I.Met.Your.Mother.S04E23.Hilfe.wider.Willen.GERMAN.DUBBED.720p.BLURAY.x264-ZZGtv',
                      'How.I.Met.Your.Mother.S01E18.GERMAN.1080p.WEB-DL.x264-X',
                      'How.I.Met.Your.Mother.S01E18.GERMAN.Multi.Subs.1080p.WEB.x264-X'):
            with self.subTest(title=title):
                self.assertEqual(_score(title)['foreign_language_penalty'], -300)

    def test_no_preferred_language_no_penalty(self):
        self.assertEqual(_score(GERMAN, preferred_language=None)['foreign_language_penalty'], 0)

    def test_upgrade_mode_ignores_it(self):
        self.assertEqual(_score(GERMAN, upgrade_mode=True)['foreign_language_penalty'], 0)


if __name__ == '__main__':
    unittest.main()
