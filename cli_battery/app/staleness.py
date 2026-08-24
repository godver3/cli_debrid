from datetime import datetime, timedelta, timezone


# Staleness thresholds
_RETURNING_SHOW_THRESHOLD = timedelta(hours=6)
_ENDED_SHOW_THRESHOLD = timedelta(days=30)
_MOVIE_THRESHOLD = timedelta(days=90)
_NULL_AIRDATE_RECHECK = timedelta(hours=24)
_TMDB_MAPPING_THRESHOLD = timedelta(days=21)


def is_stale(item_type: str, media_status: str | None, last_trakt_fetch: datetime | None, force: bool = False) -> bool:
    """Determine whether an item's metadata should be re-fetched from Trakt.

    Args:
        item_type: 'show' or 'movie'.
        media_status: Denormalized status string (e.g. 'returning series', 'ended').
        last_trakt_fetch: When the item was last fetched from Trakt API.
        force: Skip the age check entirely and report stale regardless of
            last_trakt_fetch (used by manual "refresh now" actions).

    Returns:
        True if the item is stale and should be refreshed.
    """
    if force:
        return True

    if last_trakt_fetch is None:
        return True

    now = datetime.now(timezone.utc)
    if last_trakt_fetch.tzinfo is None:
        last_trakt_fetch = last_trakt_fetch.replace(tzinfo=timezone.utc)
    age = now - last_trakt_fetch

    if item_type == 'show':
        if media_status in ('returning series', 'in production'):
            return age >= _RETURNING_SHOW_THRESHOLD
        return age >= _ENDED_SHOW_THRESHOLD

    # movie or anything else
    return age >= _MOVIE_THRESHOLD


def is_older_than(checked_at: datetime | None, max_age: timedelta) -> bool:
    """Return whether a cached value is missing or older than ``max_age``."""
    if checked_at is None:
        return True
    now = datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return (now - checked_at) >= max_age


def should_recheck_null_airdate(checked_at: datetime | None) -> bool:
    """Should we re-query Trakt for an episode whose first_aired is NULL?"""
    if checked_at is None:
        return True
    now = datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return (now - checked_at) >= _NULL_AIRDATE_RECHECK


def is_tmdb_mapping_stale(updated_at: datetime | None) -> bool:
    """Should a TMDB-to-IMDB mapping be refreshed?"""
    if updated_at is None:
        return True
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (now - updated_at) >= _TMDB_MAPPING_THRESHOLD
