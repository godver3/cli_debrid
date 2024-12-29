import logging
from guessit import guessit
from typing import List, Dict, Any, Tuple

class AnimeMatcher:
    def __init__(self, calculate_absolute_episode_func):
        logging.info("AnimeMatcher initialized")
