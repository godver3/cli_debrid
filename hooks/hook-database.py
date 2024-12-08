from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Collect all submodules
hiddenimports = collect_submodules('database')

# Collect all data files (if any)
datas = collect_data_files('database')