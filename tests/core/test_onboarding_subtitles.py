"""Optional onboarding step 5a: subtitles."""
import os
from unittest import mock

import pytest

pytest.importorskip('subliminal')


def _routes():
    import database  # noqa: F401  (app import order)
    from routes import onboarding_routes
    return onboarding_routes


@pytest.fixture
def settings():
    store = {}

    def get(section, key=None, default=None):
        sec = store.get(section, {})
        return sec if key is None else sec.get(key, default)

    def put(section, key, value):
        store.setdefault(section, {})[key] = value
    with mock.patch('utilities.settings.get_setting', side_effect=get), \
            mock.patch('utilities.settings.set_setting', side_effect=put):
        yield store


def test_off_only_turns_downloads_off(settings):
    settings['Subtitle Settings'] = {'opensubtitles_username': 'keep', 'enable_subtitles': True}
    assert _routes()._save_subtitle_step({'enable': False}) == {'enabled': False}
    assert settings['Subtitle Settings'] == {'opensubtitles_username': 'keep', 'enable_subtitles': False}


def test_on_saves_logins_and_adds_their_providers(settings):
    out = _routes()._save_subtitle_step({'enable': True, 'languages': 'eng, spa', 'os_username': 'me',
                                         'os_password': 'pw', 'subdl_key': 'k1', 'subsource_key': ''})
    sub = settings['Subtitle Settings']
    assert sub['enable_subtitles'] is True and sub['subtitle_languages'] == 'eng,spa'
    assert (sub['opensubtitles_username'], sub['opensubtitles_password'], sub['subdl_api_key']) == ('me', 'pw', 'k1')
    assert 'subsource_api_key' not in sub                      # blank: skipped
    assert out['providers'][0] == 'opensubtitlescom' and 'subdl' in out['providers']
    assert 'subsource' not in out['providers']


def test_blank_secret_keeps_saved_one(settings):
    settings['Subtitle Settings'] = {'opensubtitles_password': 'old', 'subsource_api_key': 'ss',
                                     'subtitle_providers': ['podnapisi']}
    out = _routes()._save_subtitle_step({'enable': True, 'languages': 'eng', 'os_username': 'me',
                                         'os_password': '', 'subdl_key': '', 'subsource_key': ''})
    sub = settings['Subtitle Settings']
    assert sub['opensubtitles_password'] == 'old' and sub['subsource_api_key'] == 'ss'
    assert out['providers'] == ['opensubtitlescom', 'podnapisi', 'subsource']


def test_bad_input_is_refused(settings):
    r = _routes()
    with pytest.raises(ValueError, match='language'):
        r._save_subtitle_step({'enable': True, 'languages': ' '})
    with pytest.raises(ValueError, match='xxq'):
        r._save_subtitle_step({'enable': True, 'languages': 'eng,xxq'})
    with pytest.raises(ValueError, match='password'):
        r._save_subtitle_step({'enable': True, 'languages': 'eng', 'os_username': 'me', 'os_password': ''})
    assert 'enable_subtitles' not in settings.get('Subtitle Settings', {})


def test_login_test_uses_saved_values_for_blank_fields(settings):
    from utilities import downsub
    settings['Subtitle Settings'] = {'opensubtitles_username': 'me', 'opensubtitles_password': 'pw',
                                     'subdl_api_key': 'k'}
    with mock.patch.object(downsub, 'test_provider', return_value=(True, 'ok', False)) as tp:
        assert _routes()._test_subtitle_login('opensubtitles', {})[0]
        tp.assert_called_with('opensubtitlescom', {'username': 'me', 'password': 'pw'})
        _routes()._test_subtitle_login('subdl', {'api_key': ''})
        tp.assert_called_with('subdl', {'api_key': 'k'})
    assert not _routes()._test_subtitle_login('subsource', {})[0]  # nothing saved, nothing typed


def test_provider_test_reports_refused_key():
    from subliminal.exceptions import AuthenticationError
    from utilities import downsub

    class Refusing:
        def __init__(self, **kw):
            pass

        def initialize(self):
            pass

        def list_subtitles(self, video, languages):
            raise AuthenticationError('SubdlProvider: API key refused')

        def terminate(self):
            pass
    plugin = mock.MagicMock()
    plugin.plugin = Refusing
    with mock.patch('subliminal.extensions.provider_manager', {'subdl': plugin}):
        ok, message, warning = downsub.test_provider('subdl', {'api_key': 'bad'})
    assert not ok and 'refused' in message and not warning


def test_step_template_parses():
    import jinja2
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'templates')
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(root))
    env.get_template('onboarding_step_5a.html')
    env.get_template('onboarding_navigation.html')
