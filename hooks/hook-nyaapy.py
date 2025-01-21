from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all('nyaapy')
hiddenimports += collect_submodules('nyaapy')
hiddenimports += ['lxml', 'requests']  # Add nyaapy's dependencies 