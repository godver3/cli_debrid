"""A release marked SDR is not HDR.

PTT reports "SDR" in a release name as hdr=['SDR']; a truthiness check read
that as HDR, so Netflix SD WEB-DLs (e.g. Trailer Park Boys S06E01 480p NF
WEB-DL ... SDR AV1-GRiMM) were rejected as HDR content when HDR was off.
"""
import pytest
from PTT import parse_title

from utilities.hdr_tags import hdr_tags_mean_hdr


@pytest.mark.parametrize('value,expected', [
    (None, False), ([], False), ('', False), (['SDR'], False), ('SDR', False), (['sdr'], False),
    (['HDR'], True), (['HDR10'], True), (['HDR10+'], True), (['DV', 'HDR'], True), (['SDR', 'HDR'], True), ('HDR', True),
])
def test_hdr_tags(value, expected):
    assert hdr_tags_mean_hdr(value) is expected


@pytest.mark.parametrize('title,expected', [
    ('Trailer.Park.Boys.S06E01.Way.of.the.Road.480p.NF.WEB-DL.DDP2.0.SDR.AV1-GRiMM', False),
    ('Trailer Park Boys S06 480p NF WEB-DL DDP2 0 SDR AV1-GRiMM', False),
    ('Movie.2020.1080p.WEB-DL.x264', False),
    ('Movie.2020.2160p.WEB-DL.HDR10.x265', True),
    ('Movie.2020.2160p.DV.HDR.x265', True),
])
def test_real_release_names_through_ptt(title, expected):
    assert hdr_tags_mean_hdr(parse_title(title).get('hdr')) is expected


def test_detect_hdr_uses_it():
    import database  # noqa: F401  (app import order)
    from scraper.functions.file_processing import detect_hdr
    assert detect_hdr({'hdr': ['SDR']}) is False
    assert detect_hdr({'hdr': ['HDR10']}) is True
