"""api_calls.log must not carry a Stremio addon's config path segment(s):
Comet/Torrentio configs can hold a debrid key, base64'd past the value scrubber."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import database  # noqa: F401  (before routes: debrid <-> database import cycle)
from routes import api_tracker


def _logged(url):
    logger = MagicMock()
    with patch.object(api_tracker, 'api_logger', logger, create=True), \
            patch.object(api_tracker, '_internal_hosts', return_value=set()):
        api_tracker._log_outbound(SimpleNamespace(method='GET', url=url))
    return logger.info.call_args[0][0]


def test_addon_config_hidden_from_api_log():
    line = _logged('https://comet.example/eyJkZWJyaWRBcGlLZXkiOiJTRUNSRVQifQ==/stream/movie/tt1.json')
    assert line == 'GET comet.example/<config>/stream/movie/tt1.json'
    assert _logged('https://torrentio.example/sort=x|realdebrid=KEY/manifest.json') == \
        'GET torrentio.example/<config>/manifest.json'


def test_plain_paths_and_query_strings_unchanged():
    assert _logged('https://api.example/rest/1.0/torrents?token=abc') == 'GET api.example/rest/1.0/torrents'
    assert _logged('https://comet.example/stream/movie/tt1.json') == 'GET comet.example/stream/movie/tt1.json'
