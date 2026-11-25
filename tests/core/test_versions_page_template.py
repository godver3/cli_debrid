"""The Versions settings page renders every version setting exactly once.

settings.js saves a version by collecting every named input inside its card, so
a setting the template drops, or renders twice, would be lost or overwritten on
the next save. The page groups settings under headings; this checks the grouping
still renders each one.
"""
import os
import re

import jinja2
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Saved from the filter lists rather than from named inputs, or never shown.
NOT_NAMED_INPUTS = {'filter_in', 'filter_out', 'preferred_filter_in', 'preferred_filter_out',
                    'hybrid_mode', 'scraper_priorities'}
# Rendered for every version whether or not the config has them yet.
ALWAYS_RENDERED = {'fallback_version', 'fallback_at_scrape_time', 'fallback_after_attempts',
                   'enable_scraper_priorities'}


def _render(settings):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, 'templates')),
                             autoescape=True)
    env.filters['is_infinite'] = lambda v: v == float('inf')
    env.globals['url_for'] = lambda endpoint, **kw: f"/{endpoint}/{kw.get('filename', '')}"
    return env.get_template('settings_tabs/scraping.html').render(
        settings=settings, version_names=list(settings['Scraping']['versions']))


@pytest.fixture
def version_config():
    from utilities.settings_schema import SETTINGS_SCHEMA
    schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
    config = {k: v['default'] for k, v in schema.items() if 'default' in v}
    config['display_name'] = 'My 1080p'
    config['some_future_setting'] = 7     # unknown keys still have to be editable
    config['Torrentio_1'] = 5              # per-version scraper priority
    config['filter_out'] = [{'pattern': 'CAM', 'source': 'both'}]
    config['preferred_filter_in'] = [['REMUX', 50]]
    return config


def test_every_version_setting_has_exactly_one_input(version_config):
    settings = {'Scraping': {'versions': {'1080p': version_config, '4K "HDR"': dict(version_config)}},
                'Scrapers': {'Torrentio_1': {'enabled': True}}}
    html = _render(settings)
    for version in settings['Scraping']['versions']:
        escaped = re.escape(str(jinja2.utils.markupsafe.escape(version)))
        names = re.findall(rf'name="Scraping\.versions\.{escaped}\.([^"]+)"', html)
        expected = (set(version_config) - NOT_NAMED_INPUTS) | ALWAYS_RENDERED
        assert sorted(names) == sorted(expected), version


def test_settings_land_in_their_groups(version_config):
    html = _render({'Scraping': {'versions': {'1080p': version_config}},
                    'Scrapers': {'Torrentio_1': {'enabled': True}}})

    def group(name):
        start = html.index(f'data-group="{name}"')
        return html[start:html.index('</section>', start)]

    assert 'versions.1080p.max_resolution"' in group('quality')
    assert 'versions.1080p.similarity_threshold"' in group('matching')
    assert 'versions.1080p.bitrate_weight"' in group('ranking')
    assert 'versions.1080p.fallback_version"' in group('fallback')
    assert 'data-filter-type="filter_out"' in group('filters')
    assert 'data-filter-type="preferred_filter_in"' in group('preferred')
    assert 'versions.1080p.Torrentio_1"' in group('scrapers')
    assert 'versions.1080p.some_future_setting"' in group('other')
    assert 'versions.1080p.display_name"' in group('other')
