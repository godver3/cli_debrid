import pytest


@pytest.fixture
def plex_bench(app_db, app_settings, tmp_path, monkeypatch):
    """Build a PlexBench: plex_bench(**options) -> PlexBench (see bench.py)."""
    from tests.plex_bench.bench import PlexBench
    made = []

    def build(**options):
        bench = PlexBench(tmp_path, monkeypatch, app_settings, **options)
        made.append(bench)
        return bench

    yield build
    for bench in made:
        bench.close()
