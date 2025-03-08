# -*- mode: python ; coding: utf-8 -*-

import os
import importlib.util
import tld
import sys

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
    'utilities/config'  # Ensure config directory is included
]

for directory in directories:
    dir_path = os.path.join(base_dir, directory)
    if os.path.exists(dir_path) and os.path.isdir(dir_path):
        # For utilities/config, include all .py files explicitly
        if directory == 'utilities/config':
            for file in os.listdir(dir_path):
                if file.endswith('.py'):
                    data_files.append((os.path.join(directory, file), directory))
        else:
            data_files.append((directory, directory))

# Add individual files
individual_files = [
    ('version.txt', '.'),
    ('branch_id', '.'),
    ('tooltip_schema.json', '.'),
    ('main.py', '.'),
    ('cli_battery/main.py', 'cli_battery'),
    ('optional_default_versions.json', '.'),
    ('utilities/config/downsub_config.py', 'utilities/config'),
    ('utilities/config/__init__.py', 'utilities/config')  # Explicitly include __init__.py
]

for src, dst in individual_files:
    if os.path.exists(os.path.join(base_dir, src)):
        data_files.append((src, dst))

# Add tld resource file
tld_path = os.path.dirname(importlib.util.find_spec('tld').origin)
tld_res_path = os.path.join(tld_path, 'res', 'effective_tld_names.dat.txt')
if os.path.exists(tld_res_path):
    data_files.append((tld_res_path, os.path.join('tld', 'res')))

# Create logs directory in the package
logs_dir = os.path.join(base_dir, 'logs')
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)
data_files.append(('logs', 'logs'))

# Convert relative paths to absolute paths
datas = [(os.path.join(base_dir, src) if not os.path.isabs(src) else src, dst) for src, dst in data_files]

# Additional hidden imports for resource monitoring and socket handling
additional_imports = [
    'psutil',
    'gc',
    'datetime',
    'multiprocessing.pool',
    'multiprocessing.managers',
    'multiprocessing.popen_spawn_win32',  # Windows-specific multiprocessing
    'multiprocessing.synchronize',
    'multiprocessing.heap',
    'multiprocessing.queues',
    'multiprocessing.connection',
    'multiprocessing.context',
    'multiprocessing.reduction',
    'multiprocessing.resource_tracker',
    'multiprocessing.spawn',
    'multiprocessing.util',
    'multiprocessing.forkserver',
    'multiprocessing.process',
    'multiprocessing.shared_memory',
    'multiprocessing.dummy',
    'select',
    'socket',
    'threading',
    'queue',
    'concurrent.futures',
    'concurrent.futures.thread',
    'concurrent.futures.process',
    'pkg_resources.py2_warn',
    'appdirs',
    'encodings.idna',  # For socket hostname resolution
    'encodings.utf_8',
    'encodings.ascii',
    'encodings.latin_1',
    'urllib.parse',
    'urllib.request',
    'urllib.error',
    'http.client',
    'email.message',
    'email.parser',
    'email.feedparser',
    'email.errors',
    'email.utils',
    'email.charset',
    'email.encoders',
    'email.header',
    'email.base64mime',
    'email.quoprimime',
    'email.contentmanager',
    'email.headerregistry',
    'email.iterators',
    'email.generator',
    'email.policy',
    'email._policybase',
    'email._encoded_words',
    'email._header_value_parser',
    'ssl',
    'certifi',
    'chardet',
    'idna',
    'urllib3',
    'urllib3.contrib',
    'urllib3.util',
    'urllib3.util.retry',
    'urllib3.util.timeout',
    'urllib3.util.url',
    'urllib3.util.wait',
    'urllib3.util.response',
    'urllib3.util.request',
    'urllib3.util.ssl_',
    'urllib3.util.connection',
    'urllib3.util.proxy',
    'urllib3.util.queue',
    'urllib3.util.ssltransport',
    'urllib3.connection',
    'urllib3.connectionpool',
    'urllib3.poolmanager',
    'urllib3.response',
    'urllib3.request',
    'urllib3.filepost',
    'urllib3.fields',
    'urllib3.exceptions',
    'urllib3._collections',
    'urllib3._version',
]

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
        'subliminal.refiners.tvdb',
        'subliminal.refiners.hash',
        'subliminal.providers',
        'subliminal.providers.addic7ed',
        'subliminal.providers.opensubtitles',
        'subliminal.providers.opensubtitlescom',
        'subliminal.providers.podnapisi',
        'subliminal.providers.subscenter',
        'subliminal.providers.thesubdb',
        'subliminal.providers.tvsubtitles',
        'subliminal.providers.napiprojekt',
        'subliminal.providers.gestdown',
        'subliminal.providers.legendastv',
        'subliminal.providers.shooter',
        'subliminal.providers.argenteam',
        'subliminal.score',
        'subliminal.subtitle',
        'subliminal.video',
        'utilities',
        'utilities.config',  # Ensure utilities.config is included
        'utilities.config.downsub_config',
        'utilities.post_processing',  # Add related modules
        'utilities.downsub',  # Add related modules
        'dogpile.cache',
        'dogpile.cache.api',
        'dogpile.cache.region',
        'dogpile.cache.memory',
        'dogpile.cache.backends',
        'dogpile.cache.backends.memory',
        'dogpile.core'
    ] + additional_imports,
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

# Add version information for Windows
version_info = None
if sys.platform.startswith('win'):
    try:
        with open('version.txt', 'r') as f:
            version_str = f.read().strip()
        
        # Parse version string (assuming format like "1.2.3")
        version_parts = version_str.split('.')
        if len(version_parts) >= 3:
            # Create a VSVersionInfo structure that PyInstaller expects
            from PyInstaller.utils.win32.versioninfo import (
                FixedFileInfo, StringFileInfo, StringTable,
                StringStruct, VarFileInfo, VarStruct, VSVersionInfo
            )
            
            # Convert version parts to integers with fallback to 0
            try:
                ver_major = int(version_parts[0])
                ver_minor = int(version_parts[1])
                ver_patch = int(version_parts[2])
                ver_build = 0
            except (IndexError, ValueError):
                ver_major, ver_minor, ver_patch, ver_build = 1, 0, 0, 0
                
            version_info = VSVersionInfo(
                ffi=FixedFileInfo(
                    # filevers and prodvers should be always a tuple with four items
                    filevers=(ver_major, ver_minor, ver_patch, ver_build),
                    prodvers=(ver_major, ver_minor, ver_patch, ver_build),
                    # Contains a bitmask that specifies the valid bits 'flags'
                    mask=0x3f,
                    # Contains a bitmask that specifies the Boolean attributes of the file.
                    flags=0x0,
                    # The operating system for which this file was designed.
                    # 0x4 - NT and there is no need to change it.
                    OS=0x40004,
                    # The general type of file.
                    # 0x1 - the file is an application.
                    fileType=0x1,
                    # The function of the file.
                    # 0x0 - the function is not defined for this fileType
                    subtype=0x0,
                    # Creation date and time stamp.
                    date=(0, 0)
                ),
                kids=[
                    StringFileInfo([
                        StringTable(
                            '040904B0',
                            [
                                StringStruct('CompanyName', 'CLI Debrid'),
                                StringStruct('FileDescription', 'CLI Debrid Application'),
                                StringStruct('FileVersion', f'{ver_major}.{ver_minor}.{ver_patch}.{ver_build}'),
                                StringStruct('InternalName', 'cli_debrid'),
                                StringStruct('LegalCopyright', '© 2023 CLI Debrid'),
                                StringStruct('OriginalFilename', 'cli_debrid.exe'),
                                StringStruct('ProductName', 'CLI Debrid'),
                                StringStruct('ProductVersion', f'{ver_major}.{ver_minor}.{ver_patch}.{ver_build}'),
                            ]
                        )
                    ]),
                    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
                ]
            )
    except Exception as e:
        print(f"Error setting version info: {e}")

exe = EXE(
    pyz,
    a.scripts,
    [],  # Changed from a.binaries to use collect_all
    exclude_binaries=True,  # Changed to True for collect_all
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
    icon=icon_path,
    version=version_info
)

# Collect all binaries and data files
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='cli_debrid'
)
