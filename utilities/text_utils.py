import unicodedata

# Unicode code point ranges covering Latin-script letters (base Latin plus the
# extended blocks used by accented/diacritic Latin text, e.g. Vietnamese).
# Anything else letter-like (CJK, Hangul, Devanagari, Cyrillic, Arabic, Thai,
# etc.) falls outside these ranges.
_LATIN_LETTER_RANGES = (
    (0x0041, 0x005A), (0x0061, 0x007A),  # Basic Latin A-Z a-z
    (0x00C0, 0x00FF),  # Latin-1 Supplement letters
    (0x0100, 0x017F),  # Latin Extended-A
    (0x0180, 0x024F),  # Latin Extended-B
    (0x1E00, 0x1EFF),  # Latin Extended Additional (Vietnamese etc.)
    (0x2C60, 0x2C7F),  # Latin Extended-C
    (0xA720, 0xA7FF),  # Latin Extended-D
)


def has_non_latin_letter(text: str) -> bool:
    """Return True if `text` contains any Unicode *letter* character outside
    the Latin script ranges above - script-agnostic (Japanese, Chinese,
    Korean, Hindi, Cyrillic, Arabic, Thai, etc. all match), unlike checking
    for specific script blocks one at a time.

    Only flags actual letters (Unicode category starting with 'L') - digits,
    punctuation, and spaces never trigger this, so e.g. a title with a stray
    ASCII digit alongside non-Latin letters is still correctly flagged.
    """
    if not text:
        return False
    for ch in text:
        if unicodedata.category(ch).startswith('L') and not any(
            lo <= ord(ch) <= hi for lo, hi in _LATIN_LETTER_RANGES
        ):
            return True
    return False
