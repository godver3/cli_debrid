"""PyInstaller hook for PTT package."""
from PyInstaller.utils.hooks import collect_data_files

# Collect all data files from PTT package
datas = collect_data_files('PTT', include_py_files=False)
