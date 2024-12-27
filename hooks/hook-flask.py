from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('flask')
hiddenimports += [
    'flask.json',
    'flask.json.tag',
    'flask.cli',
    'flask.helpers',
    'flask.app',
    'flask.blueprints',
]
datas = collect_data_files('flask')