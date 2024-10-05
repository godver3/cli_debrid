from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('urllib3')
datas = collect_data_files('urllib3')