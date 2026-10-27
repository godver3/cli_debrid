"""Audio-description releases rank below normal ones by default. Repro: Kitsune's
'... with Audio Description ...' WEB-DLs (Slow Horses S04E04/S05E05, By Any Means)
ranked like any other WEB-DL and were collected, giving a narration track for blind
viewers. The per-version audio_description_penalty (default 10000) is subtracted;
a negative setting makes it a bonus, 0 disables it."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from PTT import parse_title

from scraper.functions.rank_results import rank_result_key
from utilities.settings_schema import SETTINGS_SCHEMA

AD = 'Slow Horses S04E04 Returns with Audio Description 1080p ATVP WEB-DL DDP5 1 Atmos H 264-Kitsune'
NORMAL = 'Slow.Horses.S04E04.1080p.ATVP.WEB-DL.DDP5.1.Atmos.H.264-NTb'


def _score(title, query='Slow Horses', year=2022, season=4, episode=4, content_type='episode', **settings):
    version = {'max_resolution': '1080p', 'language_code': 'en'}
    version.update(settings)
    parsed = parse_title(title)
    parsed['resolution_rank'] = 3
    result = {'title': title, 'original_title': title, 'parsed_info': parsed, 'size': 1.2, 'bitrate': 6000}
    rank_result_key(result, [result], query, year, season, episode, False, content_type, version,
                    preferred_language='en')
    return result['score_breakdown']


class AudioDescriptionPenaltyTests(unittest.TestCase):
    def test_default_penalty(self):
        self.assertEqual(_score(AD)['audio_description_penalty'], -10000)
        self.assertEqual(_score(NORMAL)['audio_description_penalty'], 0)
        self.assertLess(_score(AD)['total_score'], _score(NORMAL)['total_score'])

    def test_setting_scales_disables_and_boosts(self):
        self.assertEqual(_score(AD, audio_description_penalty=500)['audio_description_penalty'], -500)
        self.assertEqual(_score(AD, audio_description_penalty=0)['audio_description_penalty'], 0)
        self.assertEqual(_score(AD, audio_description_penalty=-200)['audio_description_penalty'], 200)

    def test_naming_variants(self):
        for title in ('By.Any.Means.2026.with.Audio.Description.1080p.AMZN.WEB-DL.DDP5.1.H.264-Kitsune',
                      'By.Any.Means.2026.1080p.WEB-DL.AudioDescription.DDP5.1-GRP',
                      'By.Any.Means.2026.1080p.WEB-DL.Audio-Described.DDP5.1-GRP',
                      'By.Any.Means.2026.Descriptive.Audio.1080p.WEB-DL-GRP',
                      'By.Any.Means.2026.Described.Video.1080p.WEB-DL-GRP'):
            with self.subTest(title=title):
                score = _score(title, query='By Any Means', year=2026, season=None, episode=None,
                               content_type='movie')
                self.assertEqual(score['audio_description_penalty'], -10000)

    def test_phrase_folded_into_parsed_title(self):
        # No year before the phrase, so PTT reads it as part of the title.
        for title in ('By Any Means with Audio Description 2026 1080p AMZN WEB-DL DDP5 1 H 264-Kitsune',
                      'By.Any.Means.with.Audio.Description.1080p.AMZN.WEB-DL-Kitsune'):
            with self.subTest(title=title):
                self.assertIn('Audio Description', parse_title(title)['title'])
                score = _score(title, query='By Any Means', year=2026, season=None, episode=None,
                               content_type='movie')
                self.assertEqual(score['audio_description_penalty'], -10000)

    def test_titles_and_bare_ad_tag_not_penalized(self):
        cases = [('Ad.Astra.2019.1080p.BluRay.x264-SPARKS', 'Ad Astra', 2019),
                 ('Audio.Description.Explained.2023.1080p.WEB-DL-GRP', 'Audio Description Explained', 2023),
                 ('Some.Movie.2024.1080p.WEB-DL.DDP5.1.AD.H.264-GRP', 'Some Movie', 2024)]
        for title, query, year in cases:
            with self.subTest(title=title):
                score = _score(title, query=query, year=year, season=None, episode=None, content_type='movie')
                self.assertEqual(score['audio_description_penalty'], 0)

    def test_schema_default(self):
        schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']['audio_description_penalty']
        self.assertEqual(schema['default'], 10000)


if __name__ == '__main__':
    unittest.main()
