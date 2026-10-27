"""The release-date refresh only holds a movie for its physical date when the
version has require_physical_release, like the Wanted and Unreleased queues.

Repro: By Any Means (digital 2026-10-20, physical 2026-12-15) on a 1080p
version without require_physical_release was moved Wanted -> Unreleased by the
refresh, then back to Wanted by the Unreleased queue, on every refresh run."""
from datetime import date

import pytest

import metadata.metadata as md

TODAY = date(2026, 10, 27)
VERSIONS = {'1080p': {'require_physical_release': False}, 'REMUX': {'require_physical_release': True}}


@pytest.fixture(autouse=True)
def versions(monkeypatch):
    monkeypatch.setattr(md, 'get_setting',
                        lambda section, key, default=None: VERSIONS if (section, key) == ('Scraping', 'versions') else default)


def _state(version, release, physical, **item):
    return md._movie_refresh_state({'version': version, **item}, release, physical, TODAY)


def test_digital_out_disc_later_is_wanted_without_physical_requirement():
    assert _state('1080p', '2026-10-20', '2026-12-15') == 'Wanted'


def test_primary_version_marker_is_stripped():
    assert _state('REMUX*', '2026-10-20', '2026-12-15') == 'Unreleased'


def test_physical_requirement_waits_for_disc():
    assert _state('REMUX', '2026-10-20', '2026-12-15') == 'Unreleased'
    assert _state('REMUX', '2026-10-20', '2026-10-27') == 'Wanted'


def test_physical_requirement_without_physical_date_is_unreleased():
    assert _state('REMUX', '2026-10-20', None) == 'Unreleased'


def test_future_release_date_is_unreleased():
    assert _state('1080p', '2026-11-03', None) == 'Unreleased'


def test_unknown_dates_are_unreleased():
    assert _state('1080p', 'Unknown', None) == 'Unreleased'


def test_early_release_is_wanted():
    assert _state('REMUX', '2026-11-03', '2026-12-15', early_release=True) == 'Wanted'


def test_unknown_version_uses_release_date():
    assert _state('Gone', '2026-10-20', '2026-12-15') == 'Wanted'
