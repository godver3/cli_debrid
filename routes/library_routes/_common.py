"""Library blueprint and batch-size constants shared by the library route modules."""

from flask import Blueprint

library_bp = Blueprint('library', __name__, url_prefix='/library')

# Constants for pagination
ITEMS_PER_BATCH = 50  # Items to load per infinite scroll batch

MAX_BATCH_SIZE = 200  # Maximum items allowed per request
