"""Release-name cases parsett 1.8 reads differently from 1.7, as Clyde consumes them."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PTT import parse_title
from scraper.functions.file_processing import get_resolution_rank
from scraper.functions.filter_results import resolution_filter
from utilities.reverse_parser import _compare_resolutions_from_ptt


class PttUpgradeCases(unittest.TestCase):
    def test_four_digit_season_is_a_season_not_a_year(self):
        p = parse_title('Formula1.S2025E86.1080p.WEB.h264-ETHEL')
        self.assertEqual(p['title'], 'Formula1')
        self.assertEqual(p['seasons'], [2025])
        self.assertEqual(p['episodes'], [86])
        self.assertIsNone(p.get('year'))

    def test_oddparents_title_not_cut_at_odd(self):
        p = parse_title('The.Fairly.OddParents.S2025E01.720p.WEB')
        self.assertEqual(p['title'], 'The Fairly OddParents')

    def test_title_with_op_keeps_the_full_title(self):
        p = parse_title('Takt.Op.Destiny.S01E01.1080p.WEB-DL.x264')
        self.assertEqual(p['title'], 'Takt Op Destiny')

    def test_year_titles_are_movies(self):
        for name, title, year in (
            ('Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX.HDR.DV.HEVC.Atmos-FGT', 'Blade Runner 2049', 2017),
            ('1917.2019.1080p.BluRay.x264-SPARKS', '1917', 2019),
            ('2012.2009.1080p.BluRay.x265-RARBG', '2012', 2009),
        ):
            p = parse_title(name)
            self.assertEqual((p['title'], p['year']), (title, year), name)
            self.assertFalse(p.get('seasons') or p.get('episodes'), name)

    def test_xxx_the_movie_is_not_adult(self):
        p = parse_title('xXx.2002.1080p.BluRay.x264-GRP')
        self.assertEqual(p['title'], 'xXx')
        self.assertFalse(p.get('adult'))

    def test_yy_mm_dd_date(self):
        self.assertEqual(parse_title('Show.24.05.12.1080p.WEB.h264-GRP')['date'], '2024-05-12')

    def test_interlaced_resolution_ranks_like_progressive(self):
        # parsett 1.8 reports '1080i' where 1.7 said '1080p'
        self.assertEqual(parse_title('Movie.2020.1080i.HDTV')['resolution'], '1080i')
        self.assertEqual(get_resolution_rank('1080i'), get_resolution_rank('1080p'))
        self.assertEqual(get_resolution_rank('720i'), get_resolution_rank('720p'))
        self.assertTrue(resolution_filter('1080i', '1080p', '=='))
        self.assertTrue(resolution_filter('1080i', '1080p', '<='))
        self.assertFalse(resolution_filter('1080i', '720p', '<='))
        self.assertTrue(_compare_resolutions_from_ptt('1080i', '1080p', '=='))


if __name__ == '__main__':
    unittest.main()


def test_interlaced_resolution_reads_as_progressive():
    from scraper.functions.ptt_parser import parse_with_ptt, progressive_resolution
    assert progressive_resolution('1080i') == '1080p'
    assert progressive_resolution('576I') == '576p'
    assert progressive_resolution('2160p') == '2160p'
    assert progressive_resolution(None) is None
    # 1.8 returns '1080i' raw; our standard parse keeps the 1.7 contract
    assert parse_with_ptt('Some.Show.S01E01.1080i.HDTV.H264-GRP')['resolution'] == '1080p'
