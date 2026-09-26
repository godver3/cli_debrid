"""Newznab ID searches must drop results the indexer tags with a different title.

Live report: NZBPlanet answered t=tvsearch&imdbid=0121955&season=1&ep=5 (South
Park S01E05) with other shows' S01E05 releases, and a South Park item downloaded
The.Penguin.S01E05.
"""
import importlib.util
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOUTH_PARK = "0121955"
PENGUIN = "15435876"


def _item(title, imdb=None, guid="g"):
    attrs = '<newznab:attr name="size" value="1073741824"/>'
    if imdb is not None:
        attrs += f'<newznab:attr name="imdb" value="{imdb}"/>'
    return (f"<item><title>{title}</title><guid>{guid}</guid>"
            f'<link>https://indexer.test/getnzb/{guid}.nzb</link>'
            f'<enclosure url="https://indexer.test/getnzb/{guid}.nzb" length="1073741824" type="application/x-nzb"/>'
            f"<pubDate>Thu, 25 Sep 2026 10:00:00 +0000</pubDate>{attrs}</item>")


def _feed(*items):
    return ('<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            f"<channel>{''.join(items)}</channel></rss>")


@pytest.fixture
def newznab(monkeypatch):
    requests_seen = []

    class _Resp:
        status_code = 200

        def __init__(self, text, url):
            self.text = text
            self.url = url

    def fake_get(url, params=None, timeout=None, **kw):
        params = dict(params or {})
        requests_seen.append(params)
        if params.get("t") == "tvsearch":  # the indexer ignores imdbid and returns every S01E05
            body = _feed(
                _item("The.Penguin.S01E05.Homecoming.2160p.BluRay.HEVC-Vyndros", imdb=PENGUIN, guid="penguin"),
                _item("South.Park.S01E05.720p.BluRay.DD5.1.x264-DON", imdb=SOUTH_PARK, guid="sp-tagged"),
                _item("South.Park.S01E05.2160p.WEB.H265-SPAMnEGGS", imdb=None, guid="sp-untagged"),
            )
        else:
            body = _feed()
        return _Resp(body, url)

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    _stub("routes.api_tracker", api=types.SimpleNamespace(get=fake_get))
    _stub("utilities.settings", get_setting=lambda *a, **k: k.get("default") if "default" in k else (a[2] if len(a) > 2 else None))
    _stub("PTT", parse_title=lambda t, *a, **k: {"title": t.split(".S0")[0].replace(".", " ")})
    _stub("scraper", __path__=[str(REPO_ROOT / "scraper")])
    spec = importlib.util.spec_from_file_location("_newznab_under_test", REPO_ROOT / "scraper" / "newznab.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - sandbox missing an extra dep
        pytest.skip(f"newznab import needs an unstubbed dependency: {exc}")
    monkeypatch.setattr(module, "_get_retention_days", lambda: 0, raising=False)
    return module, requests_seen


def _scrape(module):
    return module.scrape_newznab_instance(
        "NZBPlanet_1", {"url": "https://indexer.test", "api_key": "k"},
        f"tt{SOUTH_PARK}", "South Park", 1997, "episode", season=1, episode=5,
    )


def test_other_show_from_id_search_is_dropped(newznab):
    module, _ = newznab
    titles = {r["title"] for r in _scrape(module)}
    assert "The.Penguin.S01E05.Homecoming.2160p.BluRay.HEVC-Vyndros" not in titles
    assert "South.Park.S01E05.720p.BluRay.DD5.1.x264-DON" in titles
    assert "South.Park.S01E05.2160p.WEB.H265-SPAMnEGGS" in titles  # untagged: kept, title filter decides


def test_id_search_asks_for_extended_attrs(newznab):
    module, seen = newznab
    _scrape(module)
    id_requests = [p for p in seen if p.get("t") == "tvsearch"]
    assert id_requests and all(str(p.get("extended")) == "1" for p in id_requests)


def test_imdb_normalisation(newznab):
    module, _ = newznab
    assert module._normalize_imdb("tt0121955") == module._normalize_imdb("0121955") == "121955"
    assert module._normalize_imdb("0") == ""
    assert module._normalize_imdb("") == ""
