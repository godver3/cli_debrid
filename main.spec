# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('Q:\\version.txt', '.'), ('Q:\\venv\\Lib\\site-packages\\babelfish\\data', 'babelfish/data'), ('Q:\\templates', 'templates'), ('Q:\\cli_battery', 'cli_battery'), ('Q:\\database', 'database'), ('Q:\\content_checkers', 'content_checkers'), ('Q:\\debrid', 'debrid'), ('Q:\\metadata', 'metadata'), ('Q:\\queues', 'queues'), ('Q:\\routes', 'routes'), ('Q:\\scraper', 'scraper'), ('Q:\\static', 'static'), ('Q:\\utilities', 'utilities'), ('Q:\\static\\favicon.png', 'static'), ('Q:\\tooltip_schema.json', '.')],
    hiddenimports=['database', 'database.core', 'database.collected_items', 'database.blacklist', 'database.schema_management', 'database.poster_management', 'database.statistics', 'database.wanted_items', 'database.database_reading', 'database.database_writing', '.MetaData', '.config', '.main', 'content_checkers.trakt', 'logging_config', 'main', 'metadata.Metadata'],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['Q:\\static\\favicon.png'],
)
