"""The Simple overlay preset offered in onboarding."""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SimplePresetTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'overlays', 'preset_layouts', 'simple.json')) as f:
            self.preset = json.load(f)

    def test_preset_is_valid(self):
        from overlays.layout_validator import LayoutValidator
        ok, errors = LayoutValidator().validate_layout(self.preset['layout_json'])
        self.assertTrue(ok, errors)
        self.assertEqual(self.preset['media_type'], 'both')

    def test_logos_it_uses_are_bundled(self):
        for badge in self.preset['layout_json']['badges']:
            path = (badge.get('icon') or {}).get('path')
            if path:
                with self.subTest(path=path):
                    self.assertTrue(os.path.isfile(os.path.join(ROOT, 'overlays', 'assets', path.lstrip('/'))))


if __name__ == '__main__':
    unittest.main()
