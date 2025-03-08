from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Collect all submodules of multiprocessing
hiddenimports = collect_submodules('multiprocessing')

# Add Windows-specific modules
hiddenimports += [
    'multiprocessing.popen_spawn_win32',
    'multiprocessing.synchronize',
    'multiprocessing.heap',
    'multiprocessing.queues',
    'multiprocessing.connection',
    'multiprocessing.context',
    'multiprocessing.reduction',
    'multiprocessing.resource_tracker',
    'multiprocessing.spawn',
    'multiprocessing.util',
    'multiprocessing.forkserver',
    'multiprocessing.process',
    'multiprocessing.shared_memory',
    'multiprocessing.dummy',
]

# Collect data files
datas = collect_data_files('multiprocessing') 