# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from PyInstaller.config import CONF

block_cipher = None

# Get the path to your project directory
project_path = CONF['specpath']

# Collect all files in the project directory
added_files = []
for root, dirs, files in os.walk(project_path):
    if '.git' in dirs:
        dirs.remove('.git')  # don't visit git directories
    if '__pycache__' in dirs:
        dirs.remove('__pycache__')  # don't visit pycache directories
    for file in files:
        if file.endswith(('.py', '.json', '.txt')):  # specify exact extensions
            file_path = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, project_path)
            added_files.append((file_path, relative_path))

a = Analysis(
    ['windows_wrapper.py'],
    pathex=[project_path],
    binaries=[],
    datas=added_files + [('version.txt', '.'), ('cli_battery', 'cli_battery')],
    hiddenimports=[
        'requests',
        'urllib3',
        'chardet',
        'certifi',
        'idna',
        'charset_normalizer',
        'requests.packages.urllib3',
        'requests.packages.urllib3.util',
        'requests.packages.urllib3.contrib',
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