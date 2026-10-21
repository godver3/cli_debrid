"""Deciding whether a title is anime.

Everything anime-specific (absolute numbering, romaji searches, SeaDex, anime
folders, the anime filter modes) keys off an 'anime' genre on the item. That
genre used to come only from the metadata provider's tags, so Japanese
animation tagged just "Animation" got none of it. Now:

1. a series type set on the show page (show_overrides) wins;
2. otherwise an 'anime' genre;
3. otherwise an 'animation' genre on a title whose TMDB original language is
   Japanese, unless the library already holds the title without the anime
   genre.
"""
import logging
from typing import Dict, Iterable, Optional, Tuple

_ORIGINAL_LANGUAGE_CACHE: Dict[Tuple[str, str], str] = {}


def original_language(tmdb_id, content_type) -> Optional[str]:
    """TMDB original_language ('ko' for Parasite), cached per process. Failures
    aren't cached and return None."""
    if not tmdb_id:
        return None
    media_type = 'movie' if (content_type or '').lower() == 'movie' else 'tv'
    key = (str(tmdb_id), media_type)
    if key not in _ORIGINAL_LANGUAGE_CACHE:
        try:
            from metadata.metadata import get_tmdb_metadata
            language = ((get_tmdb_metadata(str(tmdb_id), media_type) or {}).get('original_language') or '').lower()
        except Exception as e:
            logging.warning(f"Could not get original language for TMDB {media_type} {tmdb_id}: {e}")
            return None
        if not language:
            return None
        _ORIGINAL_LANGUAGE_CACHE[key] = language
    return _ORIGINAL_LANGUAGE_CACHE[key]


def series_type_override(imdb_id: Optional[str]) -> Optional[str]:
    """'anime' / 'standard' if set on the show page, else None."""
    if not imdb_id:
        return None
    try:
        from database.show_overrides import get_show_override
        return get_show_override(imdb_id).get('series_type')
    except Exception as e:
        logging.debug(f"Could not read show override for {imdb_id}: {e}")
        return None


def library_has_anime_rows(imdb_id: Optional[str], tmdb_id=None, is_show: bool = True) -> Optional[bool]:
    """How the library already files this title: True if any of its rows has
    the 'anime' genre, False if it has rows and none does, None if it has none
    (or the lookup failed)."""
    if not imdb_id and not tmdb_id:
        return None
    try:
        from database.core import get_db_connection
        from database.show_overrides import _parse_genres
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT genres FROM media_items WHERE type = ? AND (imdb_id = ? OR tmdb_id = ?) LIMIT 500",
                ('episode' if is_show else 'movie', imdb_id or '', str(tmdb_id) if tmdb_id else '')
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Could not read library rows for {imdb_id or tmdb_id}: {e}")
        return None
    if not rows:
        return None
    return any(str(g).lower() == 'anime' for row in rows for g in _parse_genres(row['genres']))


def is_anime(genres: Optional[Iterable], imdb_id: Optional[str] = None,
             tmdb_id=None, media_type: str = 'tv', use_override: bool = True) -> bool:
    """Whether to treat the title as anime (see module docstring).
    use_override=False gives what automatic detection alone decides.

    The Japanese-animation rule only classifies titles new to the library: a
    title already filed without the anime genre stays standard, so its next
    episode isn't filed under the anime folders while the rest stay put (set
    the series type on the show page to move a whole show)."""
    is_show = (media_type or '').lower() != 'movie'
    if is_show and use_override:
        override = series_type_override(imdb_id)
        if override:
            return override == 'anime'
    lowered = [str(g).lower() for g in (genres or [])]
    if 'anime' in lowered:
        return True
    if 'animation' in lowered and original_language(tmdb_id, 'tv' if is_show else 'movie') == 'ja':
        if library_has_anime_rows(imdb_id, tmdb_id, is_show) is False:
            logging.debug(f"{imdb_id or tmdb_id} is Japanese-language animation but already in the library "
                          f"as standard; keeping that")
            return False
        logging.info(f"Treating {imdb_id or tmdb_id} as anime: Japanese-language animation")
        return True
    return False
