from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all('nyaapy')
hiddenimports += ['nyaapy.nyaasi', 'nyaapy.nyaasi.nyaa'] 