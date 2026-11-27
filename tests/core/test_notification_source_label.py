"""Notification "Source:" lines use the configured display name, else a
readable form of the source id (not the raw "content_requester")."""
import database  # noqa: F401 - app import order

from routes.notifications import _source_label


def test_configured_name_wins():
    assert _source_label('Overseerr_1', {'Overseerr_1': 'Overseerr'}) == 'Overseerr'


def test_builtin_source_id_made_readable():
    assert _source_label('content_requester', {}) == 'Content Requester'
    assert _source_label('Magnet_Assigner', {}) == 'Magnet Assigner'


def test_empty_source_kept():
    assert _source_label(None, {}) is None
    assert _source_label('', {}) == ''
