# Import all submodules
from . import core
from . import collected_items
from . import blacklist
from . import schema_management
from . import poster_management
from . import statistics
from . import wanted_items
from . import database_reading
from . import database_writing

# Import all contents from each submodule
from database.core import *
from database.collected_items import *
from database.blacklist import *
from database.schema_management import *
from database.poster_management import *
from database.statistics import *
from database.wanted_items import *
from database.database_reading import *
from database.database_writing import *

# Use __all__ to specify everything to be exported
__all__ = (
    core.__all__ +
    collected_items.__all__ +
    blacklist.__all__ +
    schema_management.__all__ +
    poster_management.__all__ +
    statistics.__all__ +
    wanted_items.__all__ +
    database_reading.__all__ +
    database_writing.__all__
)