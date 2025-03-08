from PyInstaller.utils.hooks import collect_submodules

# Collect all submodules of socket
hiddenimports = collect_submodules('socket')

# Add related modules
hiddenimports += [
    'select',
    'selectors',
    'ssl',
    'encodings.idna',
    'encodings.utf_8',
    'encodings.ascii',
    'encodings.latin_1',
] 