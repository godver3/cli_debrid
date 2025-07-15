import importlib
import inspect

# Explicitly import commonly used functions
from .database_writing import update_media_item_state
from .database_reading import get_media_item_by_id

# List of all submodules
submodules = [
    'core',
    'collected_items',
    'blacklist',
    'schema_management',
    'poster_management',
    'statistics',
    'wanted_items',
    'database_reading',
    'database_writing',
    'maintenance',
    'not_wanted_magnets'
]

# Import all submodules
for submodule in submodules:
    globals()[submodule] = importlib.import_module(f'.{submodule}', package=__name__)

# Function to get all public names from a module
def get_public_names(module):
    return [name for name, obj in inspect.getmembers(module)
            if not name.startswith('_') and not inspect.ismodule(obj)]

# Generate __all__ for each submodule and the main package
__all__ = []
for submodule in submodules:
    module = globals()[submodule]
    module.__all__ = get_public_names(module)
    __all__.extend(module.__all__)

# Import all contents from each submodule
for submodule in submodules:
    exec(f'from .{submodule} import *')

# Explicitly add commonly used functions to __all__
__all__.extend(['update_media_item_state', 'get_media_item_by_id'])