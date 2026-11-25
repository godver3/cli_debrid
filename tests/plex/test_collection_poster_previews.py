"""Collection poster preview images: shipped with the app, regenerated only when missing.

On Docker installs with a PUID the static folder belongs to root, so the old
startup rewrite of default.jpg raised PermissionError (a traceback every start).
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database.collection_poster_renderer as cpr

SHIPPED = Path(cpr.STATIC_DIR) / 'img' / 'collection_poster_previews'


@unittest.skipUnless(cpr.PIL_AVAILABLE, 'Pillow not installed')
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / 'previews'
        shutil.copytree(SHIPPED, self.dir)
        for p in (patch.object(cpr, 'PREVIEW_DIR', self.dir),
                  patch.object(cpr, '_previews_generated', False)):
            p.start()
            self.addCleanup(p.stop)

    def mtimes(self):
        return {f.name: f.stat().st_mtime_ns for f in self.dir.iterdir()}

    def test_existing_previews_are_not_rewritten(self):
        before = self.mtimes()
        cpr.generate_preview_images()
        self.assertEqual(self.mtimes(), before)

    def test_read_only_folder_warns_instead_of_raising(self):
        (self.dir / 'default.jpg').unlink()
        with patch.object(cpr.Image, 'open', side_effect=PermissionError(13, 'denied')), \
             self.assertLogs(cpr.logger, 'WARNING') as logs:
            cpr.generate_preview_images()
        self.assertIn("Can't write preview images", logs.output[0])

    def test_regenerate_rebuilds_after_startup_generation(self):
        cpr.generate_preview_images()               # startup: sets the once-per-process guard
        with patch.dict(cpr.DESIGN_RENDERERS,
                        {k: (lambda **kw: cpr.Image.new('RGB', (10, 15))) for k in cpr.DESIGN_RENDERERS}):
            cpr.regenerate_all_previews()
        for did, info in cpr.DESIGNS.items():
            self.assertTrue((self.dir / info['preview']).exists(), info['preview'])
        self.assertLess((self.dir / 'design_1.jpg').stat().st_size, 2000)   # rebuilt, not the shipped one
