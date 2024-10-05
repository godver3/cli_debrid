from PyInstaller.utils.hooks import collect_submodules, collect_data_files
  from PyInstaller.utils.hooks import collect_submodules
  hiddenimports = collect_submodules('requests')
datas = collect_data_files('requests')