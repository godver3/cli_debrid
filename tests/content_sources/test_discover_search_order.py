"""Discover must render titles before starting its secondary person lookup."""

from pathlib import Path


def _function_body(source, name, next_name):
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def test_person_search_starts_after_primary_results_render():
    source = (Path(__file__).parents[2] / "static/js/discover.js").read_text()
    search = _function_body(source, "searchContent", "loadTrending")
    handler = _function_body(source, "handleSearch", "clearSearch")

    assert search.index("renderResults(data.results)") < search.index("loadPeopleMatches(query)")
    assert "loadPeopleMatches(query)" not in handler
    assert "hidePeopleMatches();" in handler


def test_person_match_sits_above_an_empty_title_state():
    root = Path(__file__).parents[2]
    source = (root / "static/js/discover.js").read_text()
    template = (root / "templates/discover.html").read_text()
    people = _function_body(source, "loadPeopleMatches", "renderPersonBanner")

    assert 'id="empty-state-anchor"' in template
    assert "el.insertAdjacentElement('afterend', state)" in people
    assert "restoreEmptyStatePosition();" in _function_body(
        source, "hidePeopleMatches", "hidePersonBanner"
    )
