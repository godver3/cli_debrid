# -*- mode: python ; coding: utf-8 -*-

import os
import importlib.util
import tld

block_cipher = None

# Get the absolute path of the current directory
base_dir = os.path.abspath(os.getcwd())

# Define data files
data_files = []

# Add directories
directories = [
    'templates',
    'cli_battery',
    'database',
    'content_checkers',
    'debrid',
    'metadata',
    'queues',
    'routes',
    'scraper',
    'static',
    'utilities',
    'utilities/config'  # Add the config subdirectory explicitly
]

for directory in directories:
    dir_path = os.path.join(base_dir, directory)
    if os.path.exists(dir_path) and os.path.isdir(dir_path):
        data_files.append((directory, directory))

# Add individual files
individual_files = [
    ('version.txt', '.'),
    ('branch_id', '.'),
    ('tooltip_schema.json', '.'),  # Fixed syntax error here
    ('main.py', '.'),
    ('cli_battery/main.py', 'cli_battery'),
    ('optional_default_versions.json', '.'),
    ('utilities/config/downsub_config.py', 'utilities/config')  # Add the config file explicitly
]

for src, dst in individual_files:
    if os.path.exists(os.path.join(base_dir, src)):
        data_files.append((src, dst))

# Add tld resource file
tld_path = os.path.dirname(importlib.util.find_spec('tld').origin)
tld_res_path = os.path.join(tld_path, 'res', 'effective_tld_names.dat.txt')
if os.path.exists(tld_res_path):
    data_files.append((tld_res_path, os.path.join('tld', 'res')))

# Convert relative paths to absolute paths
datas = [(os.path.join(base_dir, src) if not os.path.isabs(src) else src, dst) for src, dst in data_files]

a = Analysis(
    ['windows_wrapper.py'],
    pathex=[base_dir],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'database',
        'database.core',
        'database.collected_items',
        'database.blacklist',
        'database.schema_management',
        'database.poster_management',
        'database.statistics',
        'database.wanted_items',
        'database.database_reading',
        'database.database_writing',
        'content_checkers.trakt',
        'logging_config',
        'main',
        'metadata.Metadata',
        'flask',
        'sqlalchemy',
        'requests',
        'aiohttp',
        'bs4',
        'grpc',
        'guessit',
        'urwid',
        'plexapi',
        'PIL',
        'supervisor',
        'psutil',
        'api_tracker',
        'fuzzywuzzy',
        'fuzzywuzzy.fuzz',
        'Levenshtein',
        'pykakasi',
        'jaconv',
        'PTT',
        'PTT.adult',
        'PTT.handlers',
        'PTT.parse',
        'apscheduler',
        'apscheduler.schedulers.background',
        'nyaapy',
        'nyaapy.nyaasi',
        'nyaapy.nyaasi.nyaa',
        'tld',
        'tld.utils',
        'tld.base',
        'tld.exceptions',
        'subliminal',
        'subliminal.refiners',
        'subliminal.refiners.tmdb',
        'subliminal.refiners.metadata',
        'subliminal.refiners.omdb',
        'subliminal.providers',
        'subliminal.providers.addic7ed',
        'subliminal.providers.opensubtitles',
        'subliminal.providers.podnapisi',
        'subliminal.providers.subscenter',
        'subliminal.providers.thesubdb',
        'subliminal.providers.tvsubtitles',
        'subliminal.score',
        'subliminal.subtitle',
        'subliminal.video',
        'utilities',
        'utilities.config',
        'utilities.config.downsub_config'
    ],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Check for icon file
icon_path = None
possible_icons = ['static/white-icon-32x32.ico']
for icon in possible_icons:
    if os.path.exists(os.path.join(base_dir, icon)):
        icon_path = icon
        break

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='cli_debrid',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path
)
