"""
Plex Label Management Module

Handles adding, removing, and tracking Plex labels across multiple content sources.
Implements reference counting to ensure labels are only removed when no sources need them.
"""

import logging
import json
import time
import re
import sqlite3
from typing import Dict, List, Optional, Any, Set
from database.database_reading import get_db_connection
from database.core import retry_on_db_lock

# Rate limiting configuration
PLEX_API_DELAY = 0.1  # 100ms between API calls
MAX_LABELS_PER_MINUTE = 1000  # Increased from 300 - most Plex servers can handle this


class PlexRateLimiter:
    """Rate limiter for Plex API calls to prevent overwhelming the server

    Uses gradual slowdown instead of hard cutoff to avoid long waits:
    - Calls 1-800: Normal speed (100ms delay)
    - Calls 801-1000: Medium slowdown (500ms delay)
    - Calls 1001+: Heavy slowdown (1000ms delay)

    This prevents the 60-second waits that occurred with hard cutoff at 300 calls/min.
    """

    def __init__(self):
        self.last_call = 0
        self.calls_this_minute = 0
        self.minute_start = time.time()

    def wait_if_needed(self):
        """Wait if necessary to respect rate limits using gradual slowdown"""
        now = time.time()

        # Reset counter every minute
        if now - self.minute_start > 60:
            self.calls_this_minute = 0
            self.minute_start = now

        # Gradual slowdown instead of hard cutoff
        # This prevents long waits while still protecting the Plex server
        if self.calls_this_minute >= 1000:
            # Heavy slowdown - 1 second delay
            delay = 1.0
            logging.debug(f"[RateLimit] Heavy slowdown: {self.calls_this_minute} calls/min (1s delay)")
        elif self.calls_this_minute >= 800:
            # Medium slowdown - 500ms delay
            delay = 0.5
            if self.calls_this_minute == 800:
                logging.info(f"[RateLimit] Approaching limit: {self.calls_this_minute}/{MAX_LABELS_PER_MINUTE} calls/min (500ms delay)")
        else:
            # Normal speed - 100ms delay
            delay = PLEX_API_DELAY

        time.sleep(delay)
        self.calls_this_minute += 1


# Global rate limiter instance
_rate_limiter = PlexRateLimiter()


class ShowLabelCache:
    """Cache to track which shows have already had labels applied

    Prevents redundant Plex API calls when processing multiple episodes of the same show.
    Uses imdb_id/tmdb_id as the show identifier.
    """

    def __init__(self, ttl_seconds: int = 3600):
        """
        Args:
            ttl_seconds: Time-to-live for cache entries (default 1 hour)
        """
        self.cache = {}  # (show_id, label) -> timestamp
        self.ttl = ttl_seconds

    def was_recently_applied(self, imdb_id: Optional[str], tmdb_id: Optional[str], label: str) -> bool:
        """Check if label was recently applied to this show

        Args:
            imdb_id: IMDb ID of the show (preferred)
            tmdb_id: TMDB ID of the show (fallback)
            label: Label to check

        Returns:
            True if label was applied to this show within TTL
        """
        show_id = imdb_id or tmdb_id
        if not show_id:
            return False

        cache_key = (show_id, label)

        # Check if in cache and not expired
        if cache_key in self.cache:
            age = time.time() - self.cache[cache_key]
            if age < self.ttl:
                logging.debug(f"[LabelCache] HIT: Label '{label}' was applied to show {show_id} {age:.1f}s ago (within {self.ttl}s TTL)")
                return True
            else:
                # Expired, remove from cache
                logging.debug(f"[LabelCache] EXPIRED: Label '{label}' for show {show_id} was {age:.1f}s ago (exceeds {self.ttl}s TTL)")
                del self.cache[cache_key]

        return False

    def mark_as_applied(self, imdb_id: Optional[str], tmdb_id: Optional[str], label: str):
        """Mark label as applied to this show

        Args:
            imdb_id: IMDb ID of the show (preferred)
            tmdb_id: TMDB ID of the show (fallback)
            label: Label that was applied
        """
        show_id = imdb_id or tmdb_id
        if not show_id:
            return

        cache_key = (show_id, label)
        self.cache[cache_key] = time.time()
        logging.debug(f"[LabelCache] STORED: Label '{label}' for show {show_id}")

    def clear_expired(self):
        """Remove expired entries from cache"""
        now = time.time()
        expired_keys = [k for k, v in self.cache.items() if now - v >= self.ttl]
        for key in expired_keys:
            del self.cache[key]
        if expired_keys:
            logging.debug(f"[LabelCache] Cleared {len(expired_keys)} expired entries")


# Global show label cache instance
_show_label_cache = ShowLabelCache()


def sanitize_label(label: str) -> str:
    """
    Sanitize label for Plex compatibility

    Args:
        label: Raw label string (e.g., "john.smith@example.com", "Jane's Picks!")

    Returns:
        Sanitized label (e.g., "john_smith_example_com", "jane_s_picks")
    """
    if not label:
        return ""

    # Convert to lowercase first
    sanitized = label.lower()

    # Keep alphanumeric, spaces, hyphens, underscores
    # Replace other special chars with underscore
    sanitized = re.sub(r'[^\w\s-]', '_', sanitized)

    # Collapse multiple spaces/underscores
    sanitized = re.sub(r'[\s_]+', '_', sanitized)

    # Trim and limit length
    sanitized = sanitized.strip('_')[:50]

    if not sanitized:
        logging.warning(f"sanitize_label: input '{label}' produced an empty label after sanitization — label will be skipped")

    return sanitized


def parse_plex_labels(labels_json: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """
    Parse plex_labels JSON column into dictionary

    Args:
        labels_json: JSON string from database

    Returns:
        Dictionary mapping label names to their metadata:
        {
            "kids": {
                "sources": ["Trakt Lists_Kids", "Trakt Lists_Family"],
                "count": 2
            }
        }
    """
    if not labels_json:
        return {}

    try:
        return json.loads(labels_json)
    except (json.JSONDecodeError, TypeError) as e:
        logging.error(f"Error parsing plex_labels JSON: {e}")
        return {}


def serialize_plex_labels(labels_dict: Dict[str, Dict[str, Any]]) -> str:
    """
    Serialize plex_labels dictionary to JSON string

    Args:
        labels_dict: Dictionary of labels with metadata

    Returns:
        JSON string for database storage
    """
    if not labels_dict:
        return None

    return json.dumps(labels_dict)


def parse_content_sources(sources_json: Optional[str]) -> List[Dict[str, Any]]:
    """
    Parse content_sources JSON column into list

    Args:
        sources_json: JSON string from database

    Returns:
        List of source dictionaries:
        [
            {
                "source": "Overseerr_1",
                "detail": "john_smith",
                "labels": ["john_smith"],
                "request_id": "123",
                "added_at": "2025-01-15T10:30:00Z"
            }
        ]
    """
    if not sources_json:
        return []

    try:
        return json.loads(sources_json)
    except (json.JSONDecodeError, TypeError) as e:
        logging.error(f"Error parsing content_sources JSON: {e}")
        return []


def serialize_content_sources(sources_list: List[Dict[str, Any]]) -> str:
    """
    Serialize content_sources list to JSON string

    Args:
        sources_list: List of source dictionaries

    Returns:
        JSON string for database storage
    """
    if not sources_list:
        return None

    return json.dumps(sources_list)


@retry_on_db_lock(max_attempts=10, initial_wait=0.5, backoff_factor=2, long_execution_threshold_seconds=5.0)
def add_label_to_item(item_id: int, label: str, source_name: str, apply_to_plex: bool = True) -> bool:
    """
    Add a label to an item with source tracking

    Implements reference counting - if another source already added this label,
    increments the count but doesn't re-add to Plex.

    Args:
        item_id: Database ID of the media item
        label: Label to add (will be sanitized)
        source_name: Name of the content source adding this label
        apply_to_plex: Whether to actually apply to Plex (False for dry-run)

    Returns:
        True if label was added to Plex, False if already present
    """
    # Sanitize label
    label = sanitize_label(label)
    if not label:
        logging.warning(f"Cannot add empty label to item {item_id}")
        return False

    # Phase 1: DB work — read, compute, write, close.
    # The connection is released BEFORE the Plex API call to avoid holding it
    # open during a potentially slow HTTP request (100ms–2s), which caused
    # lock contention under concurrent load.
    conn = get_db_connection()
    conn.execute("PRAGMA busy_timeout = 30000")
    cursor = conn.cursor()
    item_title = 'unknown'

    try:
        # Get current item data
        cursor.execute('SELECT title, plex_labels, content_sources FROM media_items WHERE id = ?', (item_id,))
        row = cursor.fetchone()

        if not row:
            logging.error(f"Item {item_id} not found in database - this should not happen!")
            return False

        item_title = row['title']
        plex_labels = parse_plex_labels(row['plex_labels'])
        content_sources = parse_content_sources(row['content_sources'])

        if label not in plex_labels:
            # First source adding this label
            plex_labels[label] = {
                'sources': [source_name],
                'count': 1
            }
            logging.info(f"Label '{label}' added to database for '{item_title}' from {source_name}")
        else:
            # Another source also uses this label
            if source_name not in plex_labels[label]['sources']:
                plex_labels[label]['sources'].append(source_name)
                plex_labels[label]['count'] += 1
                logging.info(f"Label '{label}' on '{item_title}' now tracked by {source_name} (count: {plex_labels[label]['count']})")
            else:
                logging.debug(f"Label '{label}' already tracked by {source_name} on '{item_title}'")

        # Update content_sources list if source not already tracked
        if source_name not in [src['source'] for src in content_sources]:
            # Include detail so secondary-source label processing can use it without a separate backfill
            cs_entry: Dict[str, Any] = {'source': source_name, 'added_at': time.strftime('%Y-%m-%d %H:%M:%S')}
            try:
                src_label_cfg = get_label_config_for_source(source_name) or {}
                src_mode = src_label_cfg.get('label_mode', 'list_name')
                if src_mode == 'list_name':
                    from utilities.settings import get_all_settings as _gas
                    _src_cfg = _gas().get('Content Sources', {}).get(source_name, {})
                    detail_val = _src_cfg.get('display_name') or ''
                elif src_mode == 'fixed':
                    detail_val = src_label_cfg.get('fixed_label') or ''
                else:
                    detail_val = ''
                if detail_val:
                    cs_entry['detail'] = detail_val
            except Exception:
                pass  # Detail is best-effort; absence is handled gracefully by callers
            content_sources.append(cs_entry)
            logging.debug(f"Added source '{source_name}' to content_sources for item {item_id}")

        # Serialize and write to DB.
        # plex_labels_last_synced reset to NULL so periodic sync catches any missed Plex apply.
        serialized_labels = serialize_plex_labels(plex_labels)
        serialized_sources = serialize_content_sources(content_sources)
        cursor.execute(
            'UPDATE media_items SET plex_labels = ?, content_sources = ?, plex_labels_last_synced = NULL WHERE id = ?',
            (serialized_labels, serialized_sources, item_id)
        )
        conn.commit()

    except sqlite3.OperationalError as e:
        # Let database lock errors propagate to the retry decorator
        logging.warning(f"OperationalError adding label '{label}' to item {item_id}: {e}")
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in add_label_to_item: {rb_ex}")
        raise  # Re-raise for retry decorator
    except Exception as e:
        # Other non-operational errors should not be retried
        logging.error(f"Error adding label '{label}' to item {item_id}: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in add_label_to_item: {rb_ex}")
        return False
    finally:
        cursor.close()
        conn.close()  # Release DB connection before Plex API call

    # Phase 2: Plex API call — no DB connection held during HTTP I/O.
    # apply_label_to_plex manages its own connection for plex_labels_last_synced.
    added_to_plex = False
    if apply_to_plex:
        success = apply_label_to_plex(item_id, label)
        if success:
            added_to_plex = True
            logging.debug(f"Synced label '{label}' to Plex for '{item_title}'")
        else:
            logging.debug(f"Label '{label}' not synced to Plex for '{item_title}' (may already exist or item not in Plex)")
    else:
        logging.debug(f"Dry-run: Would sync label '{label}' to Plex for '{item_title}'")

    return added_to_plex


def remove_label_from_item(item_id: int, label: str, source_name: str, remove_from_plex: bool = True) -> bool:
    """
    Remove a label from an item (only if no other sources need it)

    Implements reference counting - decrements count and only removes from Plex
    when count reaches 0.

    Args:
        item_id: Database ID of the media item
        label: Label to remove
        source_name: Name of the content source removing this label
        remove_from_plex: Whether to actually remove from Plex (False for dry-run)

    Returns:
        True if label was removed from Plex, False if kept (other sources need it)
    """
    label = sanitize_label(label)
    if not label:
        return False

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get current item data
        cursor.execute('SELECT title, plex_labels FROM media_items WHERE id = ?', (item_id,))
        row = cursor.fetchone()

        if not row:
            logging.error(f"Item {item_id} not found in database")
            return False

        item_title = row['title']
        plex_labels = parse_plex_labels(row['plex_labels'])

        if label not in plex_labels:
            logging.debug(f"Label '{label}' not found on '{item_title}'")
            return False

        label_data = plex_labels[label]
        removed_from_plex = False

        if source_name in label_data['sources']:
            label_data['sources'].remove(source_name)
            label_data['count'] -= 1

            if label_data['count'] <= 0:
                # Last source using this label - remove from Plex
                if remove_from_plex:
                    success = remove_label_from_plex(item_id, label)
                    if success:
                        removed_from_plex = True
                        logging.info(f"Removed label '{label}' from '{item_title}' (no more sources need it)")
                    else:
                        logging.warning(f"Failed to remove label '{label}' from Plex for '{item_title}'")
                else:
                    logging.debug(f"Dry-run: Would remove label '{label}' from '{item_title}'")

                # Remove from tracking
                del plex_labels[label]
            else:
                # Other sources still need this label - keep it
                logging.info(f"Label '{label}' on '{item_title}' still needed by {label_data['sources']} (count: {label_data['count']})")
        else:
            logging.debug(f"Source {source_name} not tracking label '{label}' on '{item_title}'")

        # Update database
        cursor.execute(
            'UPDATE media_items SET plex_labels = ? WHERE id = ?',
            (serialize_plex_labels(plex_labels), item_id)
        )
        conn.commit()

        return removed_from_plex

    except Exception as e:
        logging.error(f"Error removing label '{label}' from item {item_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()


def apply_label_to_plex(item_id: int, label: str) -> bool:
    """
    Apply a label to an item in Plex

    Args:
        item_id: Database ID of the media item
        label: Label to add

    Returns:
        True if successful, False otherwise
    """
    from utilities.plex_functions import get_plex_item
    from database.database_reading import get_media_item_by_id

    try:
        # Get item data to check type and IDs
        item_data = get_media_item_by_id(item_id)
        if not item_data:
            logging.warning(f"Item {item_id} not found in database")
            return False

        # For episodes (TV shows), check cache BEFORE making expensive Plex API calls
        # This prevents redundant API calls when processing multiple episodes of the same show
        if item_data.get('type') == 'episode':
            imdb_id = item_data.get('imdb_id')
            tmdb_id = item_data.get('tmdb_id')

            # Check if we recently applied this label to this show
            if _show_label_cache.was_recently_applied(imdb_id, tmdb_id, label):
                logging.info(f"[LabelCache] Skipping Plex API call for item {item_id} - label '{label}' was recently applied to show {imdb_id or tmdb_id}")
                return True  # Return True because label is already on the show

        # Rate limiting
        _rate_limiter.wait_if_needed()

        # Get Plex item
        plex_item = get_plex_item(item_id)

        if not plex_item:
            # Only log at INFO level when item not found (this is the interesting case for debugging)
            item_data = get_media_item_by_id(item_id)
            logging.info(f"apply_label_to_plex: Item {item_id} NOT FOUND in Plex (IMDb: {item_data.get('imdb_id') if item_data else 'unknown'}), label '{label}' will apply when added to Plex")
            return False

        # For episodes, apply label to the parent show instead of the episode
        # Plex labels work at the show level, not individual episodes
        if plex_item.type == 'episode':
            plex_item = plex_item.show()
            logging.debug(f"Episode detected, applying label '{label}' to parent show '{plex_item.title}'")

        # Check if label already exists (avoid duplicate API call)
        existing_labels = [tag.tag for tag in plex_item.labels]
        label_already_existed = label in existing_labels

        if not label_already_existed:
            # Add label if it doesn't exist
            plex_item.addLabel(label)
            logging.debug(f"Applied label '{label}' to Plex item '{plex_item.title}'")
        else:
            logging.debug(f"Label '{label}' already exists on Plex item {plex_item.title}")

        # Update plex_labels_last_synced timestamp (whether label was added or already existed)
        try:
            import time
            from database.core import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            # For episodes, update ALL episodes of the same show to prevent redundant API calls
            # For movies, just update the single item
            if item_data.get('type') == 'episode':
                imdb_id = item_data.get('imdb_id')
                tmdb_id = item_data.get('tmdb_id')
                title = item_data.get('title')
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

                # Priority: imdb_id > tmdb_id > title (don't compare imdb_id with tmdb_id - different ID systems)
                if imdb_id:
                    cursor.execute('''
                        UPDATE media_items
                        SET plex_labels_last_synced = ?
                        WHERE type = 'episode'
                        AND imdb_id = ?
                    ''', (timestamp, imdb_id))
                    rows_affected = cursor.rowcount
                    logging.debug(f"Updated plex_labels_last_synced for {rows_affected} episode(s) using imdb_id '{imdb_id}'")
                elif tmdb_id:
                    cursor.execute('''
                        UPDATE media_items
                        SET plex_labels_last_synced = ?
                        WHERE type = 'episode'
                        AND tmdb_id = ?
                    ''', (timestamp, tmdb_id))
                    rows_affected = cursor.rowcount
                    logging.debug(f"Updated plex_labels_last_synced for {rows_affected} episode(s) using tmdb_id '{tmdb_id}'")
                else:
                    # Fallback to title if no IDs available (less reliable)
                    cursor.execute('''
                        UPDATE media_items
                        SET plex_labels_last_synced = ?
                        WHERE type = 'episode'
                        AND title = ?
                    ''', (timestamp, title))
                    rows_affected = cursor.rowcount
                    logging.debug(f"Updated plex_labels_last_synced for {rows_affected} episode(s) using title '{title}'")
            else:
                # For movies, just update the single item
                cursor.execute('''
                    UPDATE media_items
                    SET plex_labels_last_synced = ?
                    WHERE id = ?
                ''', (time.strftime('%Y-%m-%d %H:%M:%S'), item_id))
            conn.commit()
            conn.close()
        except Exception as timestamp_error:
            logging.warning(f"Failed to update plex_labels_last_synced timestamp for item {item_id}: {timestamp_error}")

        # Cache the label application for episodes to prevent redundant calls for other episodes of the same show
        if item_data.get('type') == 'episode':
            imdb_id = item_data.get('imdb_id')
            tmdb_id = item_data.get('tmdb_id')
            _show_label_cache.mark_as_applied(imdb_id, tmdb_id, label)
            logging.debug(f"[LabelCache] Marked label '{label}' as applied for show {imdb_id or tmdb_id}")

        return True

    except Exception as e:
        # Handle timeout errors with a softer warning (Plex server may be busy or unresponsive)
        error_str = str(e)
        if 'timeout' in error_str.lower() or 'timed out' in error_str.lower():
            item_data = get_media_item_by_id(item_id)
            item_title = item_data.get('title', f'item {item_id}') if item_data else f'item {item_id}'
            logging.warning(f"Plex server unavailable or overloaded while applying label '{label}' to '{item_title}' - will retry on next sync")
            return False

        # For other errors, log full details
        logging.error(f"Error applying label '{label}' to Plex for item {item_id}: {e}", exc_info=True)
        return False


def remove_label_from_plex(item_id: int, label: str) -> bool:
    """
    Remove a label from an item in Plex

    Args:
        item_id: Database ID of the media item
        label: Label to remove

    Returns:
        True if successful, False otherwise
    """
    from utilities.plex_functions import get_plex_item

    try:
        # Rate limiting
        _rate_limiter.wait_if_needed()

        # Get Plex item
        plex_item = get_plex_item(item_id)

        if not plex_item:
            logging.debug(f"Item {item_id} not found in Plex")
            return False

        # For episodes, remove label from the parent show instead of the episode
        # Plex labels work at the show level, not individual episodes
        if plex_item.type == 'episode':
            plex_item = plex_item.show()
            logging.debug(f"Episode detected, removing label '{label}' from parent show '{plex_item.title}'")

        # Remove label
        plex_item.removeLabel(label)
        logging.debug(f"Removed label '{label}' from Plex item '{plex_item.title}'")
        return True

    except Exception as e:
        logging.error(f"Error removing label '{label}' from Plex for item {item_id}: {e}", exc_info=True)
        return False


def get_items_by_label(label: str) -> List[Dict[str, Any]]:
    """
    Get all items that have a specific label

    Args:
        label: Label to search for

    Returns:
        List of item dictionaries with id, title, type, plex_labels
    """
    label = sanitize_label(label)
    if not label:
        return []

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Query items where plex_labels JSON contains the label
        cursor.execute('''
            SELECT id, title, type, year, plex_labels, content_source
            FROM media_items
            WHERE plex_labels LIKE ?
        ''', (f'%"{label}"%',))

        items = []
        for row in cursor.fetchall():
            plex_labels = parse_plex_labels(row['plex_labels'])

            # Verify label is actually in the parsed data (not just substring match)
            if label in plex_labels:
                items.append({
                    'id': row['id'],
                    'title': row['title'],
                    'type': row['type'],
                    'year': row['year'],
                    'content_source': row['content_source'],
                    'label_sources': plex_labels[label]['sources'],
                    'label_count': plex_labels[label]['count']
                })

        return items

    except Exception as e:
        logging.error(f"Error getting items by label '{label}': {e}", exc_info=True)
        return []
    finally:
        cursor.close()
        conn.close()


def get_labels_for_item(item_id: int) -> Dict[str, List[str]]:
    """
    Get all labels for an item with their source tracking

    Args:
        item_id: Database ID of the media item

    Returns:
        Dictionary mapping labels to their source list:
        {"kids": ["Trakt Lists_Kids", "Trakt Lists_Family"], "john_smith": ["Overseerr_1"]}
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT plex_labels FROM media_items WHERE id = ?', (item_id,))
        row = cursor.fetchone()

        if not row:
            return {}

        plex_labels = parse_plex_labels(row['plex_labels'])

        return {label: data['sources'] for label, data in plex_labels.items()}

    except Exception as e:
        logging.error(f"Error getting labels for item {item_id}: {e}", exc_info=True)
        return {}
    finally:
        cursor.close()
        conn.close()


def is_plex_labels_enabled_anywhere() -> bool:
    """
    Check if Plex labels are enabled in at least one content source or in AI Assistant.

    Returns:
        True if any content source or AI Assistant has Plex labels enabled, False otherwise
    """
    try:
        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        if any(source.get('plex_labels', {}).get('enabled', False) for source in content_sources.values()):
            return True

        # Also check AI Assistant plex_labels
        if settings.get('AI Assistant', {}).get('plex_labels', {}).get('enabled', False):
            return True

        return False
    except Exception as e:
        logging.error(f"Error checking if Plex labels enabled: {e}", exc_info=True)
        return False


def get_label_config_for_source(source_name: str) -> Optional[Dict[str, Any]]:
    """
    Get label configuration for a content source

    Args:
        source_name: Name of the content source (e.g., "Overseerr_1", "Trakt Lists_12")

    Returns:
        Label configuration dictionary or None if not configured
    """
    from utilities.settings import get_all_settings

    try:
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})

        if source_name not in content_sources:
            return None

        source_config = content_sources[source_name]

        # Get Plex label settings
        plex_labels_config = source_config.get('plex_labels', {})

        if not plex_labels_config.get('enabled', False):
            return None

        return plex_labels_config

    except Exception as e:
        logging.error(f"Error getting label config for source {source_name}: {e}", exc_info=True)
        return None


def determine_labels_for_item(item: Dict[str, Any]) -> List[str]:
    """
    Determine what labels should be applied to an item based on its content source

    Args:
        item: Media item dictionary

    Returns:
        List of labels to apply
    """
    from utilities.settings import get_all_settings

    content_source = item.get('content_source')
    content_source_detail = item.get('content_source_detail')

    if not content_source:
        return []

    # Handle AI Butler source using its own plex_labels config in AI Assistant settings
    if content_source == 'ai_butler':
        try:
            all_settings = get_all_settings()
            ai_plex = all_settings.get('AI Assistant', {}).get('plex_labels', {})
            if not ai_plex.get('enabled', False):
                return []
            label_mode = ai_plex.get('label_mode', 'fixed')
            if label_mode == 'fixed':
                fixed_label = ai_plex.get('fixed_label', 'AI Butler')
                if fixed_label:
                    return [sanitize_label(lbl.strip()) for lbl in fixed_label.split(',') if lbl.strip()]
        except Exception as e:
            logging.error(f"Error reading AI Butler plex_labels config: {e}")
        return []

    # Handle internal CLI Debrid sources (no config needed, but respect global enable state)
    if content_source in ['content_requestor', 'content_requester', 'Collected_1']:
        # Check if ANY content source has Plex labels enabled
        # This prevents labels on scraper adds when feature is disabled everywhere
        if not is_plex_labels_enabled_anywhere():
            logging.debug(f"Plex labels disabled globally - skipping internal source '{content_source}' label")
            return []

        # Original logic: apply label from content_source_detail
        if content_source_detail and content_source_detail.lower() != 'unknown':
            return [sanitize_label(content_source_detail)]
        else:
            logging.debug(f"DEBUG determine_labels: Internal source {content_source} has no valid detail")
            return []

    label_config = get_label_config_for_source(content_source)

    if not label_config:
        logging.debug(f"DEBUG determine_labels: No label config found for source {content_source}")
        return []

    labels = []
    label_mode = label_config.get('label_mode', 'list_name')  # 'requester', 'fixed', 'list_name'

    logging.debug(f"determine_labels: source={content_source}, mode={label_mode}, detail={repr(content_source_detail)}")

    if label_mode == 'requester' and content_source_detail:
        # Use requester name as label (for Overseerr)
        # Skip if requester is Unknown (missing requester info)
        if content_source_detail.lower() != 'unknown':
            labels.append(content_source_detail)

    elif label_mode == 'list_name':
        # Use display_name from settings as label (for Trakt Lists and other sources)
        # This ensures we use the user-configured display name (e.g., "UFC Events")
        # instead of auto-generated list names
        try:
            all_settings = get_all_settings()
            content_sources = all_settings.get('Content Sources', {})
            source_config = content_sources.get(content_source, {})
            display_name = source_config.get('display_name')

            if display_name:
                labels.append(display_name)
            elif content_source_detail:
                # Fallback to content_source_detail if display_name not available
                labels.append(content_source_detail)
        except Exception as e:
            logging.error(f"Error getting display_name for {content_source}: {e}")
            # Fallback to content_source_detail
            if content_source_detail:
                labels.append(content_source_detail)

    elif label_mode == 'fixed':
        # Use fixed label from config (supports comma-separated values)
        fixed_label = label_config.get('fixed_label')
        if fixed_label:
            # Split by comma and strip whitespace from each label
            for label in fixed_label.split(','):
                label = label.strip()
                if label:
                    labels.append(label)

    # Sanitize all labels
    return [sanitize_label(label) for label in labels if label]


def apply_labels_for_item(item: Dict[str, Any]) -> int:
    """
    Apply Plex labels to an item based on its content source configuration

    This is called when an item moves to Collected state.

    Args:
        item: Media item dictionary (must have 'id' and 'content_source')

    Returns:
        Number of labels successfully applied
    """
    item_id = item.get('id')
    item_title = item.get('title', 'Unknown')

    if not item_id:
        logging.warning("Cannot apply labels: item has no ID")
        return 0

    # GHOSTLIST CHECK: Skip applying labels to ghostlisted items
    if item.get('ghostlisted') == 1 or item.get('state') == 'Blacklisted':
        logging.info(f"⛔ Skipping label application - item {item_id} ({item_title}) is ghostlisted/blacklisted")
        return 0

    content_source = item.get('content_source')
    if not content_source:
        logging.debug(f"Item {item_id} ({item_title}) has no content_source, skipping label application")
        return 0

    logging.info(f"apply_labels_for_item called for: {item_title} (ID: {item_id}, source: {content_source})")

    # Determine labels to apply
    labels = determine_labels_for_item(item)

    if not labels:
        logging.warning(f"No labels configured for item {item_id} ({item_title}) from source {content_source}")
        return 0

    logging.info(f"Labels to apply for {item_title}: {labels}")

    # Apply each label
    labels_applied = 0
    for label in labels:
        try:
            logging.info(f"Calling add_label_to_item for '{label}' on {item_title} (ID: {item_id})")
            success = add_label_to_item(item_id, label, content_source, apply_to_plex=True)
            if success:
                labels_applied += 1
                logging.info(f"Successfully added label '{label}' to {item_title}")
            else:
                logging.warning(f"add_label_to_item returned False for '{label}' on {item_title}")
        except Exception as e:
            logging.error(f"Error applying label '{label}' to item {item_id}: {e}", exc_info=True)

    if labels_applied > 0:
        logging.info(f"Applied {labels_applied} Plex label(s) to '{item_title}': {', '.join(labels)}")
    else:
        logging.warning(f"No labels were applied to '{item_title}' even though {len(labels)} were determined")

    # Secondary sources: apply labels for any additional sources recorded in content_sources
    # These are sources that discovered this item before it was Collected (via pending_source_records path)
    # Guard: only run if plex_labels is enabled somewhere — same requirement as the primary path above
    if not is_plex_labels_enabled_anywhere():
        return labels_applied

    try:
        sec_conn = get_db_connection()
        row = sec_conn.execute('SELECT content_sources FROM media_items WHERE id = ?', (item_id,)).fetchone()
        sec_conn.close()
        additional_sources = parse_content_sources(row['content_sources'] if row else None)

        for src_entry in additional_sources:
            src_name = src_entry.get('source')
            src_detail = src_entry.get('detail')

            # Skip same-as-primary (already handled above)
            if src_name == content_source:
                continue

            # For requester-mode sources detail is the label itself — skip if missing/unknown
            # For list_name/fixed sources the label comes from config, so detail is not required
            src_label_config = get_label_config_for_source(src_name) or {}
            src_label_mode = src_label_config.get('label_mode', 'requester')
            if src_label_mode == 'requester':
                if not src_detail or src_detail.lower() == 'unknown':
                    continue

            temp_item = {'content_source': src_name, 'content_source_detail': src_detail}
            secondary_labels = determine_labels_for_item(temp_item)
            for label in secondary_labels:
                try:
                    success = add_label_to_item(item_id, label, src_name, apply_to_plex=True)
                    if success:
                        labels_applied += 1
                        logging.info(f"Applied secondary source label '{label}' from {src_name} to '{item_title}'")
                except Exception as e:
                    logging.error(f"Error applying secondary label '{label}' from {src_name} to item {item_id}: {e}")
    except Exception as e:
        logging.warning(f"Error processing secondary sources for item {item_id}: {e}")

    return labels_applied


def sync_labels_to_plex_for_item(item_id: int) -> int:
    """
    Sync all database labels to Plex for an item

    This function ensures that all labels stored in the database are also
    present in Plex. It's used for fixing sync issues where labels exist
    in the database but are missing from Plex.

    Args:
        item_id: Database ID of the media item

    Returns:
        Number of labels successfully synced to Plex
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get item data
        cursor.execute('SELECT title, plex_labels FROM media_items WHERE id = ?', (item_id,))
        row = cursor.fetchone()

        if not row:
            logging.error(f"Item {item_id} not found in database")
            return 0

        item_title = row['title']
        plex_labels = parse_plex_labels(row['plex_labels'])

        if not plex_labels:
            logging.debug(f"No labels in database for item {item_id} ({item_title})")
            return 0

        # Sync each label to Plex
        synced_count = 0
        for label in plex_labels.keys():
            try:
                success = apply_label_to_plex(item_id, label)
                if success:
                    synced_count += 1
                    logging.debug(f"Synced label '{label}' to Plex for '{item_title}'")
                else:
                    logging.debug(f"Label '{label}' sync returned False for '{item_title}' (may already exist or item not in Plex)")
            except Exception as e:
                logging.error(f"Error syncing label '{label}' to Plex for item {item_id}: {e}", exc_info=True)

        if synced_count > 0:
            logging.info(f"Synced {synced_count} label(s) to Plex for '{item_title}'")

        return synced_count

    finally:
        cursor.close()


def get_collected_items_with_pending_labels(movie_limit: int = 50, show_limit: int = 50) -> List[int]:
    """
    Get IDs of Collected items that have labels in database but haven't been synced to Plex yet

    Only returns items where:
    - plex_labels_last_synced IS NULL (never synced), OR
    - plex_labels_last_synced < last_updated (labels changed after last sync)

    Args:
        movie_limit: Maximum number of movies to return
        show_limit: Maximum number of shows (via representative episodes) to return

    Returns:
        List of item IDs
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get movies directly, and one representative episode per show (using MIN(id))
        # Database has 'movie' and 'episode' types, no 'show' type
        # Only get items that need syncing (never synced or labels updated since last sync)
        # Get 50 movies and 50 shows separately for balanced processing
        cursor.execute('''
            SELECT id, last_updated FROM (
                SELECT id, last_updated FROM media_items
                WHERE state IN ('Collected', 'Upgrading')
                AND type = 'movie'
                AND plex_labels IS NOT NULL
                AND plex_labels != 'null'
                AND plex_labels != '{}'
                AND plex_labels_last_synced IS NULL
                ORDER BY last_updated DESC
                LIMIT ?
            )

            UNION ALL

            SELECT id, last_updated FROM (
                SELECT MIN(id) as id, MAX(last_updated) as last_updated
                FROM media_items
                WHERE state IN ('Collected', 'Upgrading')
                AND type = 'episode'
                AND plex_labels IS NOT NULL
                AND plex_labels != 'null'
                AND plex_labels != '{}'
                AND plex_labels_last_synced IS NULL
                GROUP BY title, tmdb_id
                ORDER BY last_updated DESC
                LIMIT ?
            )
            ORDER BY last_updated DESC
        ''', (movie_limit, show_limit))

        return [row['id'] for row in cursor.fetchall()]

    finally:
        cursor.close()


def sync_pending_labels(max_items: int = 100) -> int:
    """
    Sync labels for Collected items that have labels in database

    This is called periodically to ensure labels are synced even if initial sync failed
    (e.g., item wasn't in Plex yet when label was first attempted)

    Args:
        max_items: Maximum number of items to process in one run (split equally between movies and shows)

    Returns:
        Total number of labels synced
    """
    try:
        # Split max_items equally between movies and shows
        movie_limit = max_items // 2
        show_limit = max_items // 2

        item_ids = get_collected_items_with_pending_labels(movie_limit=movie_limit, show_limit=show_limit)

        if not item_ids:
            logging.debug("No items with pending labels to sync")
            return 0

        logging.info(f"Syncing labels for {len(item_ids)} Collected items (up to {movie_limit} movies + {show_limit} shows)")

        total_synced = 0
        for item_id in item_ids:
            try:
                synced = sync_labels_to_plex_for_item(item_id)
                total_synced += synced
            except Exception as e:
                logging.error(f"Error syncing labels for item {item_id}: {e}")

        if total_synced > 0:
            logging.info(f"Label sync task completed: {total_synced} labels synced across {len(item_ids)} items")
        else:
            logging.debug(f"Label sync task completed: no new labels synced")

        return total_synced

    except Exception as e:
        logging.error(f"Error in sync_pending_labels: {e}", exc_info=True)
        return 0


def backfill_content_source_detail() -> Dict[str, Any]:
    """
    Backfill content_source_detail for items with NULL value or Overseerr items with 'Unknown'

    Uses the actual Plex labels configuration for each content source to determine
    what the content_source_detail should be based on the label_mode setting.

    For each source:
    - If label_mode = "fixed": Use the fixed_label value
    - If label_mode = "list_name": Use the display_name from config
    - If label_mode = "requester":
        - Agregarr: Use "Agregarr" (hardcoded)
        - Overseerr: Query Overseerr API to get actual requester by TMDB ID

    Note: This function will re-process Overseerr items that currently have 'Unknown'
    as their requester to attempt to recover actual requester names from the API.

    Returns:
        Dictionary with success status, counts, and details by source type
    """
    import time
    conn = None
    try:
        logging.info("Starting content_source_detail backfill using Plex labels config")

        # Get settings to check Plex labels configuration
        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        conn = get_db_connection()
        cursor = conn.cursor()

        # First, handle Overseerr items efficiently by processing unique TMDB IDs
        cursor.execute('''
            SELECT DISTINCT content_source, tmdb_id, type
            FROM media_items
            WHERE content_source LIKE 'Overseerr%'
            AND content_source_detail = 'Unknown'
            AND tmdb_id IS NOT NULL
        ''')

        overseerr_unique_items = cursor.fetchall()
        logging.info(f"Found {len(overseerr_unique_items)} unique Overseerr TMDB IDs to query")

        # Query Overseerr API for each unique TMDB ID and update all matching rows
        api_queries = 0
        overseerr_updated = {}

        for idx, unique_item in enumerate(overseerr_unique_items, 1):
            content_source = unique_item['content_source']
            tmdb_id = unique_item['tmdb_id']
            media_type = 'movie' if unique_item['type'] == 'movie' else 'tv'

            # Check if this source is configured and has Plex labels enabled
            if content_source not in content_sources:
                continue

            source_config = content_sources[content_source]
            plex_labels_config = source_config.get('plex_labels', {})

            if not plex_labels_config.get('enabled', False):
                continue

            label_mode = plex_labels_config.get('label_mode', 'requester')
            if label_mode != 'requester':
                continue

            # Query Overseerr API
            overseerr_url = source_config.get('url')
            overseerr_api_key = source_config.get('api_key')

            if not overseerr_url or not overseerr_api_key:
                continue

            try:
                from content_checkers.overseerr import get_overseerr_requester_by_tmdb
                requester = get_overseerr_requester_by_tmdb(
                    overseerr_url,
                    overseerr_api_key,
                    tmdb_id,
                    media_type
                )

                # Update ALL items with this content_source and tmdb_id
                cursor.execute('''
                    UPDATE media_items
                    SET content_source_detail = ?
                    WHERE content_source = ?
                    AND tmdb_id = ?
                    AND content_source_detail = 'Unknown'
                ''', (requester, content_source, tmdb_id))

                rows_updated = cursor.rowcount
                overseerr_updated[content_source] = overseerr_updated.get(content_source, 0) + rows_updated

                api_queries += 1
                logging.info(f"Updated {rows_updated} items for {content_source} TMDB {tmdb_id} with requester '{requester}'")

                # Commit every 20 items to release database lock
                if idx % 20 == 0:
                    conn.commit()
                    logging.info(f"Committed batch at {idx}/{len(overseerr_unique_items)}")

                # Rate limit
                time.sleep(0.2)

            except Exception as e:
                logging.error(f"Error querying Overseerr for TMDB {tmdb_id}: {e}")

        conn.commit()
        logging.info(f"Overseerr backfill complete: {api_queries} API queries, {sum(overseerr_updated.values())} items updated")

        # Now handle all other items (NULL values for non-Overseerr or other modes)
        cursor.execute('''
            SELECT id, title, content_source, type, tmdb_id
            FROM media_items
            WHERE content_source IS NOT NULL
            AND content_source_detail IS NULL
        ''')

        items = cursor.fetchall()
        logging.info(f"Found {len(items)} remaining items with NULL content_source_detail")

        # Backfill logic for remaining items (non-Overseerr or other label modes)
        updated_by_source = overseerr_updated.copy()  # Start with Overseerr counts
        skipped_no_config = 0
        skipped_no_labels = 0

        for idx, item in enumerate(items, 1):
            content_source = item['content_source']
            detail_value = None

            # Log progress every 100 items
            if idx % 100 == 0:
                logging.info(f"Processing remaining item {idx}/{len(items)}")

            # Special handling for internal CLI Debrid sources.
            # If the item already has a non-null, non-unknown detail (e.g., a username set when
            # user auth was enabled), preserve it — only fall back to 'CD-Discover' when blank.
            if content_source in ['content_requestor', 'content_requester']:
                existing_detail = item.get('content_source_detail')
                if existing_detail and existing_detail.lower() != 'unknown':
                    detail_value = existing_detail
                else:
                    detail_value = 'CD-Discover'
            elif content_source == 'Collected_1':
                detail_value = 'CD-Library'
            # Check if this content source exists in config
            elif content_source in content_sources:
                source_config = content_sources[content_source]

                # Check if Plex labels is enabled for this source
                plex_labels_config = source_config.get('plex_labels', {})
                if not plex_labels_config.get('enabled', False):
                    skipped_no_labels += 1
                    continue

                # Determine detail based on label_mode
                label_mode = plex_labels_config.get('label_mode', 'requester')

                if label_mode == 'fixed':
                    # Use the fixed label value
                    detail_value = plex_labels_config.get('fixed_label')

                elif label_mode == 'list_name':
                    # Use the display_name from the content source config
                    detail_value = source_config.get('display_name')

                elif label_mode == 'requester':
                    # Parse source type from content_source name
                    if '_' in content_source:
                        source_type = content_source.split('_', 1)[0]
                    else:
                        source_type = content_source

                    if source_type == 'Agregarr':
                        # Agregarr: hardcoded username
                        detail_value = 'Agregarr'
                    # Note: Overseerr requester mode is handled separately at the top of this function
                    # for efficiency (one API call per unique TMDB ID, updating all episodes at once)
            else:
                # Source not in config - skip
                skipped_no_config += 1
                continue

            # Update if we have a detail value
            if detail_value:
                cursor.execute('''
                    UPDATE media_items
                    SET content_source_detail = ?
                    WHERE id = ?
                ''', (detail_value, item['id']))

                # Track counts by source
                updated_by_source[content_source] = updated_by_source.get(content_source, 0) + 1

            # Commit every 100 items to release database lock
            if idx % 100 == 0:
                conn.commit()

        conn.commit()
        total_updated = sum(updated_by_source.values())
        cursor.close()
        conn.close()

        logging.info(f"Backfill complete! Updated {total_updated} items total: {updated_by_source}")
        logging.info(f"Overseerr: {api_queries} unique TMDB IDs queried (one API call per show/movie, all episodes updated together)")
        logging.info(f"Skipped {skipped_no_config} items (source not in config), {skipped_no_labels} items (Plex labels not enabled)")

        # Phase: backfill missing 'detail' on secondary content_sources entries
        # add_label_to_item writes entries without 'detail'; fix that retroactively
        # so apply_labels_for_item secondary-source processing can use them.
        # Only applies to list_name/fixed sources where detail = display_name from config.
        cs_detail_fixed = _backfill_secondary_content_sources_detail(settings)
        if cs_detail_fixed:
            logging.info(f"Backfilled detail on {cs_detail_fixed} secondary content_sources entries")

        return {
            'success': True,
            'total_updated': total_updated,
            'by_source': updated_by_source,
            'skipped_no_config': skipped_no_config,
            'skipped_no_labels': skipped_no_labels,
            'api_queries': api_queries,
            'secondary_sources_detail_fixed': cs_detail_fixed
        }

    except Exception as e:
        logging.error(f"Error in backfill_content_source_detail: {e}", exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return {
            'success': False,
            'error': str(e)
        }


def _backfill_secondary_content_sources_detail(all_settings: dict) -> int:
    """
    For each item whose content_sources list contains a secondary source entry
    without a 'detail' field, fill in the detail value from that source's config
    (display_name for list_name, fixed_label for fixed mode).

    This fixes entries written by add_label_to_item (which omits detail) so
    apply_labels_for_item's secondary-source path can process them correctly.

    Returns count of entries updated.
    """
    content_sources_cfg = all_settings.get('Content Sources', {})

    # Build map: source_id -> detail value (only list_name and fixed modes)
    source_detail_map = {}
    for src_id, src_cfg in content_sources_cfg.items():
        pl = src_cfg.get('plex_labels', {})
        if not isinstance(pl, dict) or not pl.get('enabled'):
            continue
        mode = pl.get('label_mode', 'requester')
        if mode == 'list_name':
            detail = src_cfg.get('display_name') or ''
        elif mode == 'fixed':
            detail = pl.get('fixed_label') or ''
        else:
            continue  # requester mode — detail is dynamic, skip
        if detail:
            source_detail_map[src_id] = detail

    if not source_detail_map:
        return 0

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, content_sources FROM media_items "
            "WHERE content_sources IS NOT NULL AND content_sources != '[]' AND content_sources != 'null' "
            "AND state IN ('Collected', 'Upgrading')"
        ).fetchall()

        updated = 0
        for row in rows:
            try:
                cs_list = parse_content_sources(row['content_sources'])
                changed = False
                for entry in cs_list:
                    src = entry.get('source')
                    if src in source_detail_map and 'detail' not in entry:
                        entry['detail'] = source_detail_map[src]
                        changed = True
                if changed:
                    conn.execute(
                        'UPDATE media_items SET content_sources = ?, plex_labels_last_synced = NULL WHERE id = ?',
                        (serialize_content_sources(cs_list), row['id'])
                    )
                    updated += 1
            except Exception:
                pass

        conn.commit()
        return updated
    except Exception as e:
        logging.error(f"Error in _backfill_secondary_content_sources_detail: {e}", exc_info=True)
        return 0
    finally:
        conn.close()


def regenerate_labels_from_backfilled_details(incremental: bool = False, days_back: int = 7) -> Dict[str, Any]:
    """
    Regenerate Plex labels for Collected items using current content_source_detail.

    Args:
        incremental: If True, only sync items that need updating (not synced or recently changed)
        days_back: When incremental=True, also sync items collected in last N days (default 7)

    This is useful after backfilling content_source_detail, as it will regenerate
    labels from the updated data and sync them to Plex. Handles:
    - Overseerr items (with real requester names)
    - Internal sources (CD-Discover, CD-Library)
    - Agregarr items
    - Any other sources with Plex labels enabled

    When incremental=True:
    - Only processes items with plex_labels_last_synced IS NULL (never synced)
    - OR items collected in the last N days
    - Results in 95%+ time reduction for subsequent runs

    Returns:
        Dictionary with success status and statistics
    """
    import time
    conn = None
    try:
        mode_desc = f"incremental (last {days_back} days)" if incremental else "full"
        logging.info(f"Starting label regeneration ({mode_desc}) for items with content_source_detail")

        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get unique (content_source, tmdb_id) combinations for Collected items
        # that have content_source_detail and Plex labels enabled
        if incremental:
            # Incremental mode: Only sync items that need updating
            cursor.execute('''
                SELECT DISTINCT content_source, tmdb_id, type
                FROM media_items
                WHERE state IN ('Collected', 'Upgrading')
                AND content_source IS NOT NULL
                AND content_source_detail IS NOT NULL
                AND content_source_detail != 'Unknown'
                AND (
                    plex_labels_last_synced IS NULL
                    OR collected_at >= datetime('now', '-' || ? || ' days')
                )
            ''', (days_back,))
        else:
            # Full mode: Process all items
            cursor.execute('''
                SELECT DISTINCT content_source, tmdb_id, type
                FROM media_items
                WHERE state IN ('Collected', 'Upgrading')
                AND content_source IS NOT NULL
                AND content_source_detail IS NOT NULL
                AND content_source_detail != 'Unknown'
            ''')

        unique_items = cursor.fetchall()
        logging.info(f"Found {len(unique_items)} unique items with content_source_detail to regenerate")

        regenerated_count = 0
        items_processed = 0
        skipped_no_labels = 0
        items_to_sync = []  # Collect items for Plex syncing after DB updates

        # Phase 1: Update database labels (fast, no Plex API calls)
        for unique_item in unique_items:
            content_source = unique_item['content_source']
            tmdb_id = unique_item['tmdb_id']
            item_type = unique_item['type']

            # Check if this source has Plex labels enabled
            if content_source not in ['content_requestor', 'content_requester', 'Collected_1']:
                if content_source not in content_sources:
                    continue

                source_config = content_sources[content_source]
                plex_labels_config = source_config.get('plex_labels', {})

                if not plex_labels_config.get('enabled', False):
                    skipped_no_labels += 1
                    continue

            # Get content_source_detail and content_sources from one representative item
            cursor.execute('''
                SELECT content_source_detail, content_sources, id, title
                FROM media_items
                WHERE content_source = ?
                AND tmdb_id = ?
                AND state IN ('Collected', 'Upgrading')
                ORDER BY id ASC
                LIMIT 1
            ''', (content_source, tmdb_id))

            detail_row = cursor.fetchone()
            if not detail_row:
                continue

            detail_value = detail_row['content_source_detail']
            existing_content_sources = parse_content_sources(detail_row['content_sources'])
            item_id = detail_row['id']
            item_title = detail_row['title']

            # Use determine_labels_for_item logic to generate label
            # Create a temporary item dict to pass to the function
            temp_item = {
                'content_source': content_source,
                'content_source_detail': detail_value
            }

            labels = determine_labels_for_item(temp_item)

            if not labels:
                continue

            # Create plex_labels JSON with correct format (sources + count tracking)
            new_labels_dict = {
                label: {
                    "sources": [content_source],
                    "count": 1
                }
                for label in labels
            }
            new_labels_json = json.dumps(new_labels_dict)

            # Update content_sources list if source not already tracked
            if content_source not in [src['source'] for src in existing_content_sources]:
                existing_content_sources.append({
                    'source': content_source,
                    'added_at': time.strftime('%Y-%m-%d %H:%M:%S')
                })
                logging.debug(f"Added source '{content_source}' to content_sources for regeneration")

            new_content_sources_json = serialize_content_sources(existing_content_sources)

            # Update ALL items with this content_source + tmdb_id
            cursor.execute('''
                UPDATE media_items
                SET plex_labels = ?, content_sources = ?
                WHERE content_source = ?
                AND tmdb_id = ?
                AND state IN ('Collected', 'Upgrading')
            ''', (new_labels_json, new_content_sources_json, content_source, tmdb_id))

            rows_updated = cursor.rowcount
            regenerated_count += rows_updated
            items_processed += 1

            # Collect item for Plex syncing (we'll do this after closing DB connection)
            items_to_sync.append({
                'item_id': item_id,
                'item_title': item_title,
                'labels': labels,
                'rows_updated': rows_updated
            })

            # Commit every 100 items during DB phase for progress tracking
            if items_processed % 100 == 0:
                conn.commit()
                logging.info(f"Database phase: {items_processed}/{len(unique_items)} unique items processed")

        # Commit all database changes and close connection before Plex API calls
        conn.commit()
        cursor.close()
        conn.close()
        conn = None

        logging.info(f"Database phase complete: {regenerated_count} rows updated for {items_processed} unique items")
        logging.info(f"Starting Plex sync phase for {len(items_to_sync)} items...")

        # Phase 2: Sync labels to Plex (slow, opens its own DB connections)
        synced_count = 0
        for idx, sync_item in enumerate(items_to_sync, 1):
            item_id = sync_item['item_id']
            item_title = sync_item['item_title']
            labels = sync_item['labels']
            rows_updated = sync_item['rows_updated']

            try:
                # Apply each label to Plex (this will open its own DB connection)
                for label in labels:
                    success = apply_label_to_plex(item_id, label)
                    if success:
                        logging.info(f"[{idx}/{len(items_to_sync)}] Synced label '{label}' for {item_title} ({rows_updated} database rows)")
                    else:
                        logging.warning(f"[{idx}/{len(items_to_sync)}] Label '{label}' in DB but Plex sync failed for {item_title}")
                synced_count += 1
            except Exception as e:
                logging.error(f"Error syncing labels to Plex for {item_title}: {e}")

            # Log progress every 100 items
            if idx % 100 == 0:
                logging.info(f"Plex sync progress: {idx}/{len(items_to_sync)} items")

        mode_suffix = f" ({mode_desc} mode)" if incremental else ""
        logging.info(f"Regeneration complete{mode_suffix}: {regenerated_count} DB rows updated, {synced_count} items synced to Plex")
        logging.info(f"Skipped {skipped_no_labels} items (Plex labels not enabled)")

        # Cross-source membership pass: for items that exist under multiple content_source
        # rows in the DB (same imdb_id/tmdb_id, different content_source), populate
        # content_sources (plural) with all sources that claim that item.
        cs_populated = populate_cross_source_memberships(settings)
        if cs_populated:
            logging.info(f"Populated cross-source memberships for {cs_populated} item(s)")

        # Secondary-source pass: apply labels for sources recorded in content_sources (plural)
        # that differ from the item's primary content_source.
        # Runs after populate_cross_source_memberships so newly added entries are included.
        secondary_applied = _apply_secondary_source_labels(settings)
        if secondary_applied:
            logging.info(f"Applied {secondary_applied} secondary-source label(s) to items")

        return {
            'success': True,
            'total_regenerated': regenerated_count,
            'unique_items': items_processed,
            'synced_to_plex': synced_count,
            'skipped_no_labels': skipped_no_labels,
            'secondary_labels_applied': secondary_applied,
            'mode': mode_desc,
            'message': f'Regenerated {regenerated_count} DB rows, synced {synced_count} to Plex ({items_processed} unique TMDB IDs){mode_suffix}'
        }

    except Exception as e:
        logging.error(f"Error in regenerate_labels_from_backfilled_details: {e}", exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return {
            'success': False,
            'error': str(e)
        }

def _fetch_live_imdb_ids_for_source(src_id: str, src_cfg: dict, all_settings: dict) -> Dict[str, Set[str]]:
    """
    Fetch the full live imdb_id/tmdb_id set for a content source by calling its API.

    Returns a dict with two sets:
        {
            'movie_imdb_ids': set of imdb_id strings for movies,
            'show_tmdb_ids':  set of tmdb_id strings for shows (show-level, not episode),
            'show_imdb_ids':  set of imdb_id strings for shows (fallback when tmdb_id missing),
        }
    Returns None if the source type is not supported or the fetch fails.
    """
    src_type = src_cfg.get('type', '')
    versions = {'Default': True}  # dummy — we only need IDs, not versions

    movie_imdb_ids: Set[str] = set()
    show_tmdb_ids: Set[str] = set()
    show_imdb_ids: Set[str] = set()

    def _ingest(items):
        for item in items:
            media_type = item.get('media_type', '')
            imdb_id = item.get('imdb_id') or ''
            tmdb_id = str(item.get('tmdb_id', '') or '')
            if media_type == 'movie':
                if imdb_id:
                    movie_imdb_ids.add(imdb_id)
            elif media_type in ('tv', 'show', 'episode'):
                if tmdb_id:
                    show_tmdb_ids.add(tmdb_id)
                elif imdb_id:
                    show_imdb_ids.add(imdb_id)

    try:
        if src_type == 'Trakt Lists':
            from content_checkers.trakt import get_wanted_from_trakt_lists
            list_urls = src_cfg.get('trakt_lists', '')
            if isinstance(list_urls, str):
                list_urls = [u.strip() for u in list_urls.split(',') if u.strip()]
            for url in list_urls:
                results = get_wanted_from_trakt_lists(url, versions)
                for items, _ in results:
                    _ingest(items)

        elif src_type == 'Trakt Collection':
            from content_checkers.trakt import get_wanted_from_trakt_collection
            results = get_wanted_from_trakt_collection(versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Special Trakt Lists':
            from content_checkers.trakt import get_wanted_from_special_trakt_lists
            results = get_wanted_from_special_trakt_lists(src_cfg, versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Scrob Lists':
            from content_checkers.scrob import get_wanted_from_scrob_lists
            results = get_wanted_from_scrob_lists(src_cfg.get('scrob_list_ids', ''), versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Scrob Collection':
            from content_checkers.scrob import get_wanted_from_scrob_collection
            results = get_wanted_from_scrob_collection(versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Special Scrob Lists':
            from content_checkers.scrob import get_wanted_from_scrob_special
            results = get_wanted_from_scrob_special(src_cfg, versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'My Plex Watchlist':
            from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
            results = get_wanted_from_plex_watchlist(versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Other Plex Watchlist':
            from content_checkers.plex_watchlist import get_wanted_from_other_plex_watchlist
            username = src_cfg.get('username', '')
            token = src_cfg.get('token', '')
            if username and token:
                # Signature: (username, token, versions) — no src_id prefix
                results = get_wanted_from_other_plex_watchlist(username, token, versions)
                for items, _ in results:
                    _ingest(items)

        elif src_type == 'Adaptive List':
            from content_checkers.adaptive_list import get_wanted_from_adaptive_list
            results = get_wanted_from_adaptive_list(src_cfg, versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'MDBList':
            from content_checkers.mdb_list import get_wanted_from_mdblist_source
            # Honours the source's source_mode (public /json URL or one of the API endpoints)
            results = get_wanted_from_mdblist_source(src_cfg, versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Trakt Watchlist':
            from content_checkers.trakt import get_wanted_from_trakt_watchlist
            results = get_wanted_from_trakt_watchlist(versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'Friends Trakt Watchlist':
            from content_checkers.trakt import get_wanted_from_friend_trakt_watchlist
            results = get_wanted_from_friend_trakt_watchlist(src_cfg, versions)
            for items, _ in results:
                _ingest(items)

        elif src_type == 'My Plex RSS Watchlist':
            from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss
            rss_url = src_cfg.get('url', '')
            if rss_url:
                results = get_wanted_from_plex_rss(rss_url, versions)
                for items, _ in results:
                    _ingest(items)

        elif src_type == 'My Friends Plex RSS Watchlist':
            from content_checkers.plex_rss_watchlist import get_wanted_from_friends_plex_rss
            rss_url = src_cfg.get('url', '')
            if rss_url:
                results = get_wanted_from_friends_plex_rss(rss_url, versions)
                for items, _ in results:
                    _ingest(items)

        else:
            # Unsupported type (Overseerr/Agregarr are requester-mode, handled elsewhere)
            return None

    except Exception as e:
        logging.warning(f"[populate_cross_source_memberships] Failed to fetch live list for {src_id} ({src_type}): {e}")
        return None

    logging.info(f"[populate_cross_source_memberships] {src_id}: fetched {len(movie_imdb_ids)} movies, {len(show_tmdb_ids)} shows (tmdb), {len(show_imdb_ids)} shows (imdb) from live API")
    return {'movie_imdb_ids': movie_imdb_ids, 'show_tmdb_ids': show_tmdb_ids, 'show_imdb_ids': show_imdb_ids}


def populate_cross_source_memberships(all_settings: dict) -> int:
    """
    For each enabled list_name/fixed content source, fetch the full live item list
    from the source's API, then find all Collected items in the DB that belong to that
    source but whose primary content_source is different. For each such item, append
    this source to their content_sources list (with source, detail, added_at) if not
    already present.

    Movies matched by imdb_id. Shows/episodes matched at show level by tmdb_id
    (with imdb_id as fallback), updating all collected episodes for that show.

    Safe to run multiple times — deduplicates before appending.
    Returns count of items updated.
    """
    content_sources_cfg = all_settings.get('Content Sources', {})

    # Build map of eligible sources: src_id -> (detail, src_cfg)
    eligible = {}
    for src_id, src_cfg in content_sources_cfg.items():
        pl = src_cfg.get('plex_labels', {})
        if not isinstance(pl, dict) or not pl.get('enabled'):
            continue
        mode = pl.get('label_mode', 'requester')
        if mode == 'list_name':
            detail = src_cfg.get('display_name') or ''
        elif mode == 'fixed':
            detail = pl.get('fixed_label') or ''
        else:
            continue  # requester — skip
        if detail:
            eligible[src_id] = (detail, src_cfg)

    if not eligible:
        return 0

    conn = get_db_connection()
    total_updated = 0
    try:
        for src_id, (detail, src_cfg) in eligible.items():
            ids = _fetch_live_imdb_ids_for_source(src_id, src_cfg, all_settings)
            if ids is None:
                # Unsupported source type — fall back to DB-only membership (original behaviour)
                ids = {'movie_imdb_ids': set(), 'show_tmdb_ids': set(), 'show_imdb_ids': set()}
                db_movie_rows = conn.execute(
                    "SELECT DISTINCT imdb_id FROM media_items "
                    "WHERE content_source = ? AND state IN ('Collected', 'Upgrading') "
                    "AND imdb_id IS NOT NULL AND type = 'movie'",
                    (src_id,)
                ).fetchall()
                ids['movie_imdb_ids'] = {r['imdb_id'] for r in db_movie_rows if r['imdb_id']}

                db_show_rows = conn.execute(
                    "SELECT DISTINCT tmdb_id FROM media_items "
                    "WHERE content_source = ? AND state IN ('Collected', 'Upgrading') "
                    "AND tmdb_id IS NOT NULL AND type = 'episode'",
                    (src_id,)
                ).fetchall()
                ids['show_tmdb_ids'] = {str(r['tmdb_id']) for r in db_show_rows if r['tmdb_id']}

            now_ts = time.strftime('%Y-%m-%d %H:%M:%S')
            # SQLite default SQLITE_MAX_VARIABLE_NUMBER is 999; chunk to stay safe
            _CHUNK = 900

            def _apply_gap_rows(gap_rows):
                nonlocal total_updated
                for row in gap_rows:
                    cs_list = parse_content_sources(row['content_sources'])
                    if any(s['source'] == src_id for s in cs_list):
                        continue
                    cs_list.append({'source': src_id, 'detail': detail, 'added_at': now_ts})
                    conn.execute(
                        'UPDATE media_items SET content_sources = ?, plex_labels_last_synced = NULL WHERE id = ?',
                        (serialize_content_sources(cs_list), row['id'])
                    )
                    total_updated += 1

            # --- Movies: match by imdb_id, chunked to avoid SQLite variable limit ---
            movie_ids_list = list(ids['movie_imdb_ids'])
            for i in range(0, max(len(movie_ids_list), 1), _CHUNK):
                chunk = movie_ids_list[i:i + _CHUNK]
                if not chunk:
                    break
                placeholders = ','.join('?' * len(chunk))
                gap_rows = conn.execute(
                    f"SELECT id, content_sources FROM media_items "
                    f"WHERE imdb_id IN ({placeholders}) "
                    f"AND content_source != ? "
                    f"AND state IN ('Collected', 'Upgrading') "
                    f"AND type = 'movie'",
                    chunk + [src_id]
                ).fetchall()
                _apply_gap_rows(gap_rows)

            # --- Shows/Episodes: match at show level by tmdb_id, then imdb_id fallback ---
            show_queries = []
            if ids['show_tmdb_ids']:
                show_queries.append(('tmdb_id', list(ids['show_tmdb_ids'])))
            if ids['show_imdb_ids']:
                show_queries.append(('imdb_id', list(ids['show_imdb_ids'])))

            for id_field, id_values in show_queries:
                for i in range(0, max(len(id_values), 1), _CHUNK):
                    chunk = id_values[i:i + _CHUNK]
                    if not chunk:
                        break
                    placeholders = ','.join('?' * len(chunk))
                    gap_ep_rows = conn.execute(
                        f"SELECT id, content_sources FROM media_items "
                        f"WHERE {id_field} IN ({placeholders}) "
                        f"AND content_source != ? "
                        f"AND state IN ('Collected', 'Upgrading') "
                        f"AND type = 'episode'",
                        chunk + [src_id]
                    ).fetchall()
                    _apply_gap_rows(gap_ep_rows)

        conn.commit()
        logging.info(f"[populate_cross_source_memberships] Updated {total_updated} items across {len(eligible)} sources")
        return total_updated

    except Exception as e:
        logging.error(f"Error in populate_cross_source_memberships: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


def _apply_secondary_source_labels(all_settings: dict) -> int:
    """
    For every Collected item that has secondary sources in content_sources (plural),
    apply labels for those secondary sources if not already present.

    This covers the gap where an item was collected via source A before source B had
    plex_labels configured — source B's label was never applied because add_wanted_items
    only fires when a source actively encounters an already-collected item.

    Returns count of labels applied.
    """
    content_sources_cfg = all_settings.get('Content Sources', {})

    # Only process sources that have plex_labels enabled
    enabled_sources = {
        src_id for src_id, src_cfg in content_sources_cfg.items()
        if isinstance(src_cfg.get('plex_labels'), dict) and src_cfg['plex_labels'].get('enabled')
    }
    if not enabled_sources:
        return 0

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, content_source, content_sources, plex_labels FROM media_items "
            "WHERE state IN ('Collected', 'Upgrading') "
            "AND content_sources IS NOT NULL AND content_sources != '[]' AND content_sources != 'null'"
        ).fetchall()
    finally:
        conn.close()

    applied = 0
    for row in rows:
        try:
            cs_list = parse_content_sources(row['content_sources'])
            existing_labels = parse_plex_labels(row['plex_labels'])
            primary = row['content_source']

            for entry in cs_list:
                src_name = entry.get('source')
                src_detail = entry.get('detail')

                if not src_name or src_name == primary or src_name not in enabled_sources:
                    continue

                # Get label config for this secondary source
                src_label_config = get_label_config_for_source(src_name) or {}
                src_label_mode = src_label_config.get('label_mode', 'requester')

                # Requester mode needs detail; list_name/fixed get label from config
                if src_label_mode == 'requester':
                    if not src_detail or src_detail.lower() == 'unknown':
                        continue

                temp_item = {'content_source': src_name, 'content_source_detail': src_detail}
                labels = determine_labels_for_item(temp_item)

                for label in labels:
                    # Skip if already applied from this source
                    if label in existing_labels and src_name in existing_labels[label].get('sources', []):
                        continue
                    try:
                        success = add_label_to_item(row['id'], label, src_name, apply_to_plex=True)
                        if success:
                            applied += 1
                            logging.info(f"Secondary-source label '{label}' from '{src_name}' applied to '{row['title']}'")
                    except Exception as e:
                        logging.warning(f"Failed applying secondary label '{label}' from '{src_name}' to item {row['id']}: {e}")
        except Exception as e:
            logging.warning(f"Error processing secondary sources for item {row['id']}: {e}")

    return applied


def backfill_missing_labels() -> Dict[str, Any]:
    """
    Generate and sync Plex labels ONLY for items with NULL/empty plex_labels.

    This is useful for:
    - Items that missed label generation due to errors
    - Items added before Plex labels were enabled
    - Catching up without overwriting existing labels

    Unlike regenerate_labels_from_backfilled_details(), this:
    - Only processes items with NULL or empty plex_labels
    - Preserves existing labels on other items
    - Safe to run multiple times
    - Uses separate connections for DB updates and Plex API calls to avoid locks

    Returns:
        Dictionary with success status and statistics
    """
    import time
    conn = None
    try:
        logging.info("Starting label backfill for items with NULL/empty plex_labels")

        from utilities.settings import get_all_settings
        settings = get_all_settings()
        content_sources = settings.get('Content Sources', {})

        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all unique (content_source, tmdb_id) combinations for Collected/Upgrading items
        # that have content_source_detail but NO plex_labels AND haven't been synced
        cursor.execute('''
            SELECT DISTINCT content_source, tmdb_id, type
            FROM media_items
            WHERE state IN ('Collected', 'Upgrading')
            AND content_source IS NOT NULL
            AND content_source_detail IS NOT NULL
            AND content_source_detail != 'Unknown'
            AND (plex_labels IS NULL OR plex_labels = '')
            AND plex_labels_last_synced IS NULL
        ''')

        unique_items = cursor.fetchall()
        logging.info(f"Found {len(unique_items)} unique items with NULL/empty plex_labels to backfill")

        if len(unique_items) == 0:
            logging.info("No items need label backfill - all items already have labels")
            cursor.close()
            conn.close()
            return {
                'success': True,
                'total_backfilled': 0,
                'unique_items': 0,
                'skipped_no_labels': 0,
                'message': 'No items needed label backfill'
            }

        backfilled_count = 0
        items_processed = 0
        skipped_no_labels = 0
        skipped_no_config = {}  # Track items skipped by unconfigured source
        skipped_no_labels_by_source = {}  # Track items skipped by disabled label sources
        items_to_sync = []  # Collect items for Plex syncing after DB updates

        # Phase 1: Update database labels (fast, no Plex API calls)
        for unique_item in unique_items:
            content_source = unique_item['content_source']
            tmdb_id = unique_item['tmdb_id']
            item_type = unique_item['type']

            # Check if this source has Plex labels enabled
            if content_source not in ['content_requestor', 'content_requester', 'Collected_1']:
                if content_source not in content_sources:
                    # Track and warn about unconfigured sources
                    skipped_no_config[content_source] = skipped_no_config.get(content_source, 0) + 1
                    continue

                source_config = content_sources[content_source]
                plex_labels_config = source_config.get('plex_labels', {})

                if not plex_labels_config.get('enabled', False):
                    skipped_no_labels += 1
                    skipped_no_labels_by_source[content_source] = skipped_no_labels_by_source.get(content_source, 0) + 1
                    continue

            # Get content_source_detail and content_sources from one representative item
            cursor.execute('''
                SELECT content_source_detail, content_sources, id, title
                FROM media_items
                WHERE content_source = ?
                AND tmdb_id = ?
                AND state IN ('Collected', 'Upgrading')
                AND (plex_labels IS NULL OR plex_labels = '')
                ORDER BY id ASC
                LIMIT 1
            ''', (content_source, tmdb_id))

            detail_row = cursor.fetchone()
            if not detail_row:
                continue

            detail_value = detail_row['content_source_detail']
            existing_content_sources = parse_content_sources(detail_row['content_sources'])
            item_id = detail_row['id']
            item_title = detail_row['title']

            # Use determine_labels_for_item logic to generate label
            temp_item = {
                'content_source': content_source,
                'content_source_detail': detail_value
            }

            labels = determine_labels_for_item(temp_item)

            if not labels:
                continue

            # Create plex_labels JSON with correct format
            new_labels_dict = {
                label: {
                    "sources": [content_source],
                    "count": 1
                }
                for label in labels
            }
            new_labels_json = json.dumps(new_labels_dict)

            # Update content_sources list if source not already tracked
            if content_source not in [src['source'] for src in existing_content_sources]:
                existing_content_sources.append({
                    'source': content_source,
                    'added_at': time.strftime('%Y-%m-%d %H:%M:%S')
                })
                logging.debug(f"Added source '{content_source}' to content_sources for backfill")

            new_content_sources_json = serialize_content_sources(existing_content_sources)

            # Update ALL items with this content_source + tmdb_id that have NULL labels
            cursor.execute('''
                UPDATE media_items
                SET plex_labels = ?, content_sources = ?
                WHERE content_source = ?
                AND tmdb_id = ?
                AND state IN ('Collected', 'Upgrading')
                AND (plex_labels IS NULL OR plex_labels = '')
            ''', (new_labels_json, new_content_sources_json, content_source, tmdb_id))

            rows_updated = cursor.rowcount
            backfilled_count += rows_updated
            items_processed += 1

            # Collect item for Plex syncing (we'll do this after closing DB connection)
            items_to_sync.append({
                'item_id': item_id,
                'item_title': item_title,
                'labels': labels,
                'rows_updated': rows_updated
            })

        # Commit all database changes and close connection before Plex API calls
        conn.commit()
        cursor.close()
        conn.close()
        conn = None

        logging.info(f"Database phase complete: {backfilled_count} rows updated for {items_processed} unique items")
        logging.info(f"Starting Plex sync phase for {len(items_to_sync)} items...")

        # Phase 2: Sync labels to Plex (slow, opens its own DB connections)
        synced_count = 0
        for idx, sync_item in enumerate(items_to_sync, 1):
            item_id = sync_item['item_id']
            item_title = sync_item['item_title']
            labels = sync_item['labels']
            rows_updated = sync_item['rows_updated']

            try:
                # Apply each label to Plex (this will open its own DB connection)
                for label in labels:
                    success = apply_label_to_plex(item_id, label)
                    if success:
                        logging.info(f"[{idx}/{len(items_to_sync)}] Synced label '{label}' for {item_title} ({rows_updated} database rows)")
                    else:
                        logging.warning(f"[{idx}/{len(items_to_sync)}] Label '{label}' in DB but Plex sync failed for {item_title}")
                synced_count += 1
            except Exception as e:
                logging.error(f"Error syncing labels to Plex for {item_title}: {e}")

            # Log progress every 20 items
            if idx % 20 == 0:
                logging.info(f"Plex sync progress: {idx}/{len(items_to_sync)} items")

        logging.info(f"Backfill complete: {backfilled_count} DB rows updated, {synced_count} items synced to Plex")

        # Log warnings about unconfigured sources
        if skipped_no_config:
            total_skipped_no_config = sum(skipped_no_config.values())
            source_list = ', '.join(sorted(skipped_no_config.keys()))
            logging.warning(f"⚠ Skipped {total_skipped_no_config} items from {len(skipped_no_config)} unconfigured source(s): {source_list}")
            logging.warning(f"   → Action: Add these sources to config with Plex labels enabled, or remove items from these sources")
            for source, count in sorted(skipped_no_config.items()):
                logging.warning(f"   - {source}: {count} items")

        # Log info about sources with labels disabled
        if skipped_no_labels_by_source:
            source_list = ', '.join(sorted(skipped_no_labels_by_source.keys()))
            logging.info(f"Skipped {skipped_no_labels} items from {len(skipped_no_labels_by_source)} source(s) with Plex labels disabled: {source_list}")

        # Build message with skip details
        message_parts = [f'Backfilled {backfilled_count} DB rows, synced {synced_count} to Plex ({items_processed} unique TMDB IDs)']
        if skipped_no_config:
            message_parts.append(f'Skipped {sum(skipped_no_config.values())} items from {len(skipped_no_config)} unconfigured sources')
        if skipped_no_labels:
            message_parts.append(f'Skipped {skipped_no_labels} items (labels disabled)')

        return {
            'success': True,
            'total_backfilled': backfilled_count,
            'unique_items': items_processed,
            'synced_to_plex': synced_count,
            'skipped_no_labels': skipped_no_labels,
            'skipped_no_config': skipped_no_config,
            'skipped_no_config_count': sum(skipped_no_config.values()) if skipped_no_config else 0,
            'message': '. '.join(message_parts)
        }

    except Exception as e:
        logging.error(f"Error in backfill_missing_labels: {e}", exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return {
            'success': False,
            'error': str(e)
        }