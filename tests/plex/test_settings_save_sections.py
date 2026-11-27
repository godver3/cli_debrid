"""static/js/settings.js drops every top-level section that isn't in topLevelFields.

Plex Preset Collections toggles rendered and looked fine but never saved,
because the section was missing from that list. Every section a settings
template writes through data-section must be on it (Reverse Parser has its own
save path, see settings.js reverse parser block).
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAVED_ELSEWHERE = {'Reverse Parser'}


class SettingsSaveSectionsTests(unittest.TestCase):
    def test_every_template_section_survives_the_save_filter(self):
        js = open(os.path.join(ROOT, 'static/js/settings.js'), encoding='utf-8').read()
        match = re.search(r"const topLevelFields = \[(.*?)\];", js, re.S)
        self.assertIsNotNone(match, 'topLevelFields list not found in settings.js')
        allowed = set(re.findall(r"'([^']+)'", match.group(1)))
        used = set()
        for path in glob.glob(os.path.join(ROOT, 'templates/settings_tabs/**/*.html'), recursive=True):
            with open(path, encoding='utf-8') as f:
                # skip JS-built attributes like data-section="' + x.section + '"
                used.update(s for s in re.findall(r'data-section="([^"\'{+]+)"', f.read()))
        missing = sorted(used - allowed - SAVED_ELSEWHERE)
        self.assertEqual(missing, [], f'sections dropped on save: {missing}')


if __name__ == '__main__':
    unittest.main()
