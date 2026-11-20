"""Releases that put the episode's own words before SxxEyy.

MustBeNice, RHO Amsterdam S02 (2026-11-20): 'The.Real.Housewives.Of.Amsterdam.
Girls.Trip.Antwerpen.s02e10' scored 0.53 and every such release was rejected,
so the episode looked like it had no results. Reunions are named the same way
('Show.Reunion.Part.1.S05E17'). The added words pass only when they are the
episode's title from metadata or reunion/part words, so spin-offs stay out.
"""
import logging
import unittest
from unittest.mock import Mock

from scraper.functions.filter_results import _episode_words_after_title, filter_results
from scraper.functions.ptt_parser import parse_with_ptt

VERSION = {'resolution_wanted': '<=', 'max_resolution': '1080p', 'min_size_gb': 0.01, 'max_size_gb': 100.0,
           'filter_in': [], 'filter_out': [], 'enable_hdr': False, 'similarity_threshold': 0.8,
           'similarity_threshold_anime': 0.8, 'min_bitrate_mbps': 0.0, 'max_bitrate_mbps': 1000.0}


def run(title, show, season, episode, ep_title, aliases=None):
    api = Mock()
    api.get_show_aliases.return_value = ({}, None)
    api.get_show_metadata.return_value = ({}, None)
    api.get_show_seasons.return_value = ({season: {'episodes': {episode: {'title': ep_title}}}}, None)
    result = {'title': title, 'original_title': title, 'size': 1.5, 'parsed_info': parse_with_ptt(title),
              'scraper_type': 'Newznab', 'scraper_instance': 't', 'additional_metadata': {'filename': title}}
    passed, _ = filter_results(results=[result], tmdb_id='1', title=show, year=2022, content_type='episode',
                               season=season, episode=episode, multi=False, version_settings=VERSION, runtime=45,
                               episode_count=13, season_episode_counts={season: 20}, genres=['reality'],
                               matching_aliases=aliases or [], imdb_id='tt1', direct_api=api)
    return bool(passed), result.get('filter_reason')


class EpisodeLabelTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_episode_title_before_tag_passes(self):
        ok, why = run('The.Real.Housewives.Of.Amsterdam.Girls.Trip.Antwerpen.s02e10.1080p.WEBRip',
                      'The Real Housewives of Amsterdam', 2, 10, 'Girls Trip Antwerpen')
        self.assertTrue(ok, why)

    def test_reunion_label_passes_without_episode_title(self):
        ok, why = run('The.Real.Housewives.of.Salt.Lake.City.Reunion.Part.1.S05E17.1080p.WEB.h264-EDITH',
                      'The Real Housewives of Salt Lake City', 5, 17, '')
        self.assertTrue(ok, why)

    def test_wrong_episode_number_still_rejected(self):
        ok, _ = run('The.Real.Housewives.Of.Amsterdam.Girls.Trip.Antwerpen.s02e10.1080p.WEBRip',
                    'The Real Housewives of Amsterdam', 2, 11, 'Girls Trip Antwerpen')
        self.assertFalse(ok)

    def test_spinoff_still_rejected(self):
        ok, _ = run('Below.Deck.Mediterranean.S02E10.1080p.WEB.h264-EDITH', 'Below Deck', 2, 10, 'Charter Chaos')
        self.assertFalse(ok)

    def test_unrelated_words_rejected(self):
        ok, _ = run('The.Real.Housewives.Of.Amsterdam.Spreken.Is.Zilver.Zwijgen.Is.Goud.s02e11.1080p.WEBRip',
                    'The Real Housewives of Amsterdam', 2, 11, 'Speech Is Silver')
        self.assertFalse(ok, 'an English metadata title does not vouch for Dutch words')

    def test_helper(self):
        t = ['the.real.housewives.of.amsterdam']
        self.assertEqual(_episode_words_after_title('the.real.housewives.of.amsterdam.reunion.deel.2', t, ''), 'reunion deel 2')
        self.assertEqual(_episode_words_after_title('the.real.housewives.of.amsterdam.part.2', t, ''), '',
                         'part words alone are not a reunion label')
        self.assertEqual(_episode_words_after_title('below.deck.mediterranean', ['below.deck'], 'Mediterranean Nights'), 'mediterranean',
                         'only when metadata names the episode that way')


if __name__ == '__main__':
    unittest.main()
