"""'Plex' file management is shown as "Direct Mount"; the saved value stays 'Plex'."""
from utilities.file_management_labels import file_management_label
from utilities.settings_schema import SETTINGS_SCHEMA


def test_label_for_each_stored_value():
    assert file_management_label('Plex') == 'Direct Mount'
    assert file_management_label('Symlinked/Local') == 'Symlinked/Local'
    assert file_management_label(None) == ''
    assert file_management_label('Something else') == 'Something else'


def test_settings_dropdown_shows_label_but_keeps_stored_values():
    field = SETTINGS_SCHEMA['File Management']['file_collection_management']
    assert field['choices'] == ['Plex', 'Symlinked/Local']
    assert field['default'] == 'Plex'
    assert field['choice_labels'] == {'Plex': 'Direct Mount'}
