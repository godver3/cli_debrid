# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from PyInstaller.config import CONF

block_cipher = None

a = Analysis(
    ['windows_wrapper.py'],
    pathex=[],
    binaries=[],
    datas=[('version.txt', '.'), ('cli_battery', 'cli_battery')],
    hiddenimports=[
        'requests', 'urllib3', 'chardet', 'certifi', 'idna',
        'requests.packages.urllib3.util.retry',
        'requests.packages.urllib3.util.timeout',
        'requests.packages.urllib3.util.url',
        'requests.packages.urllib3.util.wait',
        'requests.packages.urllib3.contrib',
        'requests.packages.urllib3.contrib.pyopenssl',
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

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