from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('pykakasi')
datas = collect_data_files('pykakasi')