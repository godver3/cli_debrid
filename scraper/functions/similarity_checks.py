import html
import re
from difflib import SequenceMatcher
import unicodedata
from scraper.functions import *
from functools import lru_cache

# Pre-compiled regex patterns for better performance
_SHIELD_PATTERN = re.compile(r'S\.H\.I\.E\.L\.D\.?|S\s+H\s+I\s+E\s+L\s+D', re.IGNORECASE)
_SWAT_PATTERN = re.compile(r'S\.W\.A\.T\.?|S\s+W\s+A\s+T', re.IGNORECASE)
_PUNCTUATION_PATTERN = re.compile(r"[':()\[\]{}]")
_SPACE_PATTERN = re.compile(r'[\s_]+')
_MULTI_PERIOD_PATTERN = re.compile(r'\.+')
_BARE_039S_PATTERN = re.compile(r'\b039\s+s\b')
_BARE_039_PATTERN = re.compile(r'\s+039\s+')

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

@lru_cache(maxsize=1024)
def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods, removing certain punctuation,
    standardizing the format, and removing non-English letters while keeping accented English letters and '&'.
    Uses caching to avoid re-processing the same title multiple times.
    """
    # Decode HTML entities (handles &#039; → ', &amp; → &, etc.)
    title = html.unescape(title)
    # Also handle bare numeric remnants from partially-stripped entities (e.g. " 039 s" from &#039;s)
    title = _BARE_039S_PATTERN.sub("'s", title)
    title = _BARE_039_PATTERN.sub("'", title)
    # Legacy explicit replacements as fallback
    if '&' in title:
        title = title.replace('&039;', "'").replace('&039s', "'s").replace('&#39;', "'")

    # Convert superscript and subscript unicode digits to normal digits to improve matching (e.g., '²' -> '2')
    supersub_digit_map = str.maketrans({
        '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
        '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
        '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4',
        '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9'
    })
    title = title.translate(supersub_digit_map)
    
    # Handle percentage signs early if present
    if '%' in title:
        title = title.replace('1%', '1.percent').replace('1.%', '1.percent')
    
    # Replace slashes with spaces before normalization
    title = title.replace('/', ' ')
    
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
    
    # Insert a period between letter-digit and digit-letter boundaries (e.g., 'accountant2' -> 'accountant.2')
    normalized = re.sub(r'(?<=\d)(?=[a-zA-Z])|(?<=[a-zA-Z])(?=\d)', '.', normalized)
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)  # Clean up if new double periods were created
    
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