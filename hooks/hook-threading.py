from PyInstaller.utils.hooks import collect_submodules

# Collect all submodules of threading
hiddenimports = collect_submodules('threading')

# Add related modules
hiddenimports += [
    '_thread',
    'queue',
    'concurrent.futures',
    'concurrent.futures.thread',
    'concurrent.futures.process',
] 