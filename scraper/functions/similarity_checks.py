import logging
import re
from typing import Dict, Any
from difflib import SequenceMatcher
from fuzzywuzzy import fuzz
from guessit import guessit
import unicodedata
from scraper.functions import *
from functools import lru_cache

# Pre-compiled regex patterns for better performance
_SHIELD_PATTERN = re.compile(r'S\.H\.I\.E\.L\.D\.?', re.IGNORECASE)
_SWAT_PATTERN = re.compile(r'S\.W\.A\.T\.?|S\s+W\s+A\s+T', re.IGNORECASE)
_PUNCTUATION_PATTERN = re.compile(r"[':()\[\]{}]")
_SPACE_PATTERN = re.compile(r'[\s_]+')
_MULTI_PERIOD_PATTERN = re.compile(r'\.+')

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def improved_title_similarity(query_title: str, result: Dict[str, Any], is_anime: bool = False, content_type: str = None) -> float:
    # Normalize titles
    query_title = normalize_title(query_title).replace('&', 'and').replace('-','.')
    
    parsed_info = result.get('parsed_info', {})
    result_title = result.get('title', '')
    
    # Prepare query title for guessit
    guessit_query_title = query_title.replace('.', ' ')
    
    logging.debug(f"Original query title: '{query_title}'")
    logging.debug(f"Prepared guessit query title: '{guessit_query_title}'")
    logging.debug(f"Original result title: '{result_title}'")
    
    # Use guessit with content type and prepared query title
    guessit_type = 'movie' if content_type.lower() == 'movie' else 'episode'
    guessit_result = guessit(result_title, {'type': guessit_type, 'expected_title': [guessit_query_title]})
    
    logging.debug(f"Full guessit result: {guessit_result}")
    
    guessit_title = guessit_result.get('title', '')
    guessit_title = normalize_title(guessit_title).replace('&', 'and').replace('-','.')
    
    logging.debug(f"Final normalized guessit title: '{guessit_title}'")

    logging.debug(f"Comparing cleaned titles - Query: '{query_title}', Guessit: '{guessit_title}'")

    if is_anime:
        # For anime, use match_any_title function
        official_titles = [query_title]
        
        # Add alternative titles
        alternative_titles = parsed_info.get('alternative_title', [])
        if isinstance(alternative_titles, str):
            alternative_titles = [alternative_titles]
        official_titles.extend(alternative_titles)
        
        # Normalize alternative titles
        official_titles = [normalize_title(title).replace('&', 'and') for title in official_titles]
        
        similarity = match_any_title(guessit_title, official_titles)
        
        logging.debug(f"Anime title similarity: {similarity}")

    else:
        # For non-anime, use the existing logic with improved word matching
        token_sort_similarity = fuzz.token_sort_ratio(query_title, guessit_title) / 100
        
        # Split into words and remove 's' from the end of words for comparison
        query_words = set(word.rstrip('s') for word in query_title.split())
        guessit_words = set(word.rstrip('s') for word in guessit_title.split())
        
        # Check if all base words (without 's') are present
        all_words_present = query_words.issubset(guessit_words) or guessit_words.issubset(query_words)

        # If token sort similarity is very high (>0.95), don't penalize as heavily
        if token_sort_similarity > 0.95:
            similarity = token_sort_similarity
        else:
            similarity = token_sort_similarity * (0.75 if all_words_present else 0.5)

        logging.debug(f"Token sort ratio: {token_sort_similarity}")
        logging.debug(f"All base words present: {all_words_present}")

    logging.debug(f"Final similarity score: {similarity}")

    return similarity  # Already a float between 0 and 1

def preprocess_title(title):
    # Remove only non-resolution quality terms
    terms_to_remove = ['web-dl', 'webrip', 'bluray', 'dvdrip']
    for term in terms_to_remove:
        title = re.sub(r'\b' + re.escape(term) + r'\b', '', title, flags=re.IGNORECASE)
    # Remove any resulting double periods
    title = re.sub(r'\.{2,}', '.', title)
    # Remove any resulting double spaces
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

@lru_cache(maxsize=1024)
def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods, removing certain punctuation,
    standardizing the format, and removing non-English letters while keeping accented English letters and '&'.
    Uses caching to avoid re-processing the same title multiple times.
    """
    # Quick replacement of common HTML entities
    if '&' in title:
        title = title.replace('&039;', "'").replace('&039s', "'s").replace('&#39;', "'")
    
    # Handle percentage signs early if present
    if '%' in title:
        title = title.replace('1%', '1.percent').replace('1.%', '1.percent')
    
    # Normalize Unicode characters
    normalized = unicodedata.normalize('NFKD', title)
    
    # Handle common acronyms with pre-compiled patterns
    normalized = _SHIELD_PATTERN.sub('SHIELD', normalized)
    normalized = _SWAT_PATTERN.sub('SWAT', normalized)
    
    # Remove punctuation and convert spaces to periods in one pass
    normalized = _PUNCTUATION_PATTERN.sub('', normalized)
    normalized = _SPACE_PATTERN.sub('.', normalized)
    # Ensure single periods between words
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)
    # Add periods around standalone letters (like 'TS') that should be matched
    normalized = re.sub(r'(?<=[^.\w])(\w)(?=[^.\w])', r'.\1.', normalized)
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)  # Clean up any double periods from the previous step
    
    # Efficient character filtering using a single pass
    chars = []
    for c in normalized:
        if (c.isalnum() or 
            c in '.-&' or 
            (unicodedata.category(c).startswith('L') and ord(c) < 0x300)):
            # Only append ASCII chars and '&'
            if ord(c) < 128 or c == '&':
                chars.append(c)
    
    # Join characters and clean up
    normalized = ''.join(chars).strip('.').lower()
    
    return normalized