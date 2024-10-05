from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('babelfish')
datas = collect_data_files('babelfish')