"""Diagnostics → Rate Limits: daily limits for enabled Newznab indexers."""
from unittest import mock


def test_lists_enabled_indexers_with_their_limits(app_settings):
    app_settings['Scrapers'] = {
        'NinjaCentral_1': {'type': 'Newznab', 'enabled': True, 'api_limit_per_day': 1500,
                           'grab_limit_per_day': 350, 'limit_reset': 'midnight_utc'},
        'Old_1': {'type': 'Newznab', 'enabled': False},
        'Torrentio_1': {'type': 'Torrentio', 'enabled': True},
    }
    from flask import Flask
    import database  # noqa: F401  (app import order; routes first is circular)
    import routes.models as models
    from routes.debug_routes import debug_bp
    app = Flask(__name__)
    app.register_blueprint(debug_bp, url_prefix='/debug')
    with mock.patch.object(models, 'is_user_system_enabled', return_value=False):
        data = app.test_client().get('/debug/api/indexer_quota_info').get_json()
    assert [i['name'] for i in data['indexers']] == ['NinjaCentral_1']
    ix = data['indexers'][0]
    assert ix['api']['limit'] == 1500 and ix['grab']['limit'] == 350
    assert ix['reset'] == 'midnight_utc'
