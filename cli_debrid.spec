# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from PyInstaller.config import CONF

block_cipher = None

a = Analysis(['windows_wrapper.py'],
             pathex=[],
             binaries=[],
             datas=[('version.txt', '.'), ('cli_battery', 'cli_battery')],
             hiddenimports=[
                 'engineio.async_drivers.threading',
                 'flask',
                 'flask_session',
                 'flask_cors',
                 'flask_login',
                 'flask_sqlalchemy',
                 'sqlalchemy',
                 'aiohttp',
                 'babelfish',
                 'bs4',
                 'bencode.BTL',
                 'fuzzywuzzy',
                 'grpc',
                 'guessit',
                 'pykakasi',
                 'requests',
                 'urllib3',               # Add this line
                 'idna',                  # Add this line
                 'charset_normalizer',    # Add this line (or 'chardet' for older versions)
                 'certifi',               # Add this line
                 'tenacity',
                 'urwid',
                 'werkzeug',
                 'trakt',
                 'plexapi',
                 'colorlog',
                 'iso8601',
                 'PIL',
                 'supervisor',
                 'ntplib',
                 'parsedatetime',
                 'pytz',
                 'psutil',
                 'flask.json',
                 'flask.json.tag',
                 'flask.cli',
                 'flask.helpers',
                 'flask.app',
                 'flask.blueprints',
                 # Add any other modules that might be dynamically imported
             ],
             hookspath=['hooks'],
             hooksconfig={},
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)

exe = EXE(pyz,
          a.scripts, 
          [],
          exclude_binaries=True,
          name='cli_debrid',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          console=True,
          disable_windowed_traceback=False,
          target_arch=None,
          codesign_identity=None,
          entitlements_file=None )

# Clean up the dist directory
dist_dir = os.path.join(CONF['distpath'], 'cli_debrid')
if os.path.exists(dist_dir):
    shutil.rmtree(dist_dir, ignore_errors=True)

# Ensure the directory is created
os.makedirs(dist_dir, exist_ok=True)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='cli_debrid',
)