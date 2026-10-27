"""New versions ship with a Preferred Filter Out penalty for audio-description
releases.

Live: Kitsune's "with Audio Description" WEB-DLs (Slow Horses S04E04/S05E05,
By Any Means) ranked like any other WEB-DL and were collected, so the user got a
narration track for blind viewers instead of the normal audio.
"""
import json
import os

import pytest

from scraper.functions.other_functions import smart_search
from utilities.settings_schema import AUDIO_DESCRIPTION_PENALTY, SETTINGS_SCHEMA

PATTERN = AUDIO_DESCRIPTION_PENALTY[0]
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.parametrize("title", [
    "Slow Horses S04E04 Returns with Audio Description 1080p ATVP WEB-DL DDP5 1 Atmos H 264-Kitsune",
    "By.Any.Means.2026.with.Audio.Description.1080p.AMZN.WEB-DL.DDP5.1.H.264-Kitsune",
    "Some.Movie.2024.1080p.WEB-DL.AudioDescription.DDP5.1-GRP",
    "Some.Movie.2024.1080p.WEB-DL.Audio-Described.DDP5.1-GRP",
    "Some.Show.S01E01.Descriptive.Audio.1080p.WEB-DL-GRP",
    "Some.Show.S01E01.Described.Video.1080p.WEB-DL-GRP",
])
def test_pattern_matches_audio_description_releases(title):
    assert smart_search(PATTERN, title)


@pytest.mark.parametrize("title", [
    "Slow.Horses.S04E04.1080p.ATVP.WEB-DL.DDP5.1.Atmos.H.264-NTb",
    "Ad.Astra.2019.1080p.BluRay.x264-SPARKS",
    "A.D.The.Bible.Continues.S01E01.1080p.WEB-DL-GRP",
])
def test_pattern_ignores_normal_releases(title):
    assert not smart_search(PATTERN, title)


def test_schema_default_includes_penalty():
    default = SETTINGS_SCHEMA['Scraping']['versions']['schema']['preferred_filter_out']['default']
    assert AUDIO_DESCRIPTION_PENALTY in default


def test_optional_default_versions_include_penalty():
    with open(os.path.join(REPO, 'optional_default_versions.json')) as f:
        versions = json.load(f)['versions']
    for name, version in versions.items():
        assert version['preferred_filter_out'].count(AUDIO_DESCRIPTION_PENALTY) == 1, name
