"""Every URL the frontend calls must match a registered Flask route.

A JS fetch to a route that was renamed or removed fails quietly in the browser:
Discover's add-media page posted to /add_item and the logo picker to
/settings/save_single_setting, neither of which existed. This test collects the
literal URLs passed to fetch/$.ajax/$.get/$.post/form actions in static/js and
templates and matches each against the real app's url_map.
"""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT', 'usenet', 'overlays')

# fetch('/x'), fetch(`/x/${id}`), $.post('/x'), $.ajax({url: '/x'}), action="/x"
_CALL = re.compile(
    r"""(?:\bfetch\(\s*|\$\.(?:get|post|getJSON|ajax)\(\s*|\burl\s*:\s*|\baction\s*=\s*)"""
    r"""(['"`])(/[^'"`]*)\1(\s*\+)?""")


def _frontend_urls():
    found = []
    for folder, exts in (('static/js', ('.js',)), ('templates', ('.html',))):
        for root, _dirs, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if not name.endswith(exts):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                for m in _CALL.finditer(text):
                    url, concatenated = m.group(2), bool(m.group(3))
                    if '{{' in url or '{%' in url or url.startswith(('//', '/static/')):
                        continue
                    url = url.split('?')[0].split('#')[0]
                    # ${...} stands for any text inside a segment; a "'/x/' + id" tail can
                    # be anything after (e.g. "+ imdbId + '/seasons'")
                    parts = re.split(r'\$\{[^}]*\}', url)
                    pattern = '[^/]+'.join(re.escape(p) for p in parts)
                    sample = '1'.join(parts)
                    if concatenated and url.endswith('/'):
                        pattern += '.+'
                        sample += '1'
                    line = text.count('\n', 0, m.start()) + 1
                    found.append((url, re.compile(pattern + '/?'), sample.rstrip('/') or '/',
                                  f'{os.path.relpath(path, ROOT)}:{line}'))
    return found


class FrontendUrlsHaveRoutesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
            del sys.modules[name]
        import database  # noqa: F401  (app import order; routes first is circular)
        from flask import Flask
        from routes import register_blueprints
        app = Flask(__name__)
        register_blueprints(app)
        # Match both ways: the route as a regex against the URL with '1' for its
        # placeholders, and the URL as a regex against the route with '1' per argument.
        self.routes = []
        for rule in app.url_map.iter_rules():
            regex = ''
            for literal, arg in re.findall(r'([^<]*)(<[^>]+>)?', rule.rule):
                regex += re.escape(literal)
                if arg:
                    regex += r'\d+' if arg.startswith(('<int:', '<float:')) else (
                        '.+' if arg.startswith('<path:') else '[^/]+')
            self.routes.append((re.compile(regex.rstrip('/') + '/?'), re.sub(r'<[^>]+>', '1', rule.rule)))

    def _exists(self, pattern, sample):
        return any(route_re.fullmatch(sample) or pattern.fullmatch(route_sample)
                   or pattern.fullmatch(route_sample + '/') for route_re, route_sample in self.routes)

    def test_every_frontend_url_matches_a_route(self):
        urls = _frontend_urls()
        self.assertGreater(len(urls), 200)  # the scan found the calls at all
        missing = sorted(f'{where}: {url}' for url, pattern, sample, where in urls
                         if not self._exists(pattern, sample))
        self.assertEqual(missing, [], 'frontend calls URLs with no route:\n' + '\n'.join(missing))


if __name__ == '__main__':
    unittest.main()
