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
MAX_LABELS_PER_MINUTE = 300  # Conservative limit


class PlexRateLimiter:
    """Rate limiter for Plex API calls to prevent overwhelming the server"""

    def __init__(self):
        self.last_call = 0
        self.calls_this_minute = 0
        self.minute_start = time.time()

    def wait_if_needed(self):
        """Wait if necessary to respect rate limits"""
        now = time.time()

        # Reset counter every minute
        if now - self.minute_start > 60:
            self.calls_this_minute = 0
            self.minute_start = now

        # Hit limit? Wait for next minute
        if self.calls_this_minute >= MAX_LABELS_PER_MINUTE:
            wait_time = 60 - (now - self.minute_start)
            if wait_time > 0:
                logging.info(f"Plex rate limit reached ({MAX_LABELS_PER_MINUTE}/min), waiting {wait_time:.1f}s")
                time.sleep(wait_time)
                self.calls_this_minute = 0
                self.minute_start = time.time()

        # Standard delay between calls
        time.sleep(PLEX_API_DELAY)
        self.calls_this_minute += 1


# Global rate limiter instance
_rate_limiter = PlexRateLimiter()


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


@retry_on_db_lock(max_attempts=10, initial_wait=0.5, backoff_factor=2)
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

    conn = get_db_connection()
    # Set a timeout for database locks (30 seconds)
    # The retry decorator will handle retries if this timeout is still exceeded
    conn.execute("PRAGMA busy_timeout = 30000")
    cursor = conn.cursor()

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

        added_to_plex = False

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

        # Always attempt to sync to Plex when apply_to_plex=True
        # This ensures labels are synced even if they were previously added to DB but failed to sync
        if apply_to_plex:
            success = apply_label_to_plex(item_id, label)
            if success:
                added_to_plex = True
                logging.debug(f"Synced label '{label}' to Plex for '{item_title}'")
            else:
                logging.debug(f"Label '{label}' not synced to Plex for '{item_title}' (may already exist or item not in Plex)")
        else:
            logging.debug(f"Dry-run: Would sync label '{label}' to Plex for '{item_title}'")

        # Update content_sources list if source not already tracked
        if source_name not in [src['source'] for src in content_sources]:
            content_sources.append({'source': source_name, 'added_at': time.strftime('%Y-%m-%d %H:%M:%S')})
            logging.debug(f"Added source '{source_name}' to content_sources for item {item_id}")

        # Serialize data for database
        serialized_labels = serialize_plex_labels(plex_labels)
        serialized_sources = serialize_content_sources(content_sources)

        # Update database with both plex_labels and content_sources
        cursor.execute(
            'UPDATE media_items SET plex_labels = ?, content_sources = ? WHERE id = ?',
            (serialized_labels, serialized_sources, item_id)
        )
        conn.commit()

        return added_to_plex

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
        conn.close()


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
        # Rate limiting
        _rate_limiter.wait_if_needed()

        # Get Plex item
        plex_item = get_plex_item(item_id)

        if not plex_item:
            # Only log at INFO level when item not found (this is the interesting case for debugging)
            item_data = get_media_item_by_id(item_id)
            logging.info(f"apply_label_to_plex: Item {item_id} NOT FOUND in Plex (IMDb: {item_data.get('imdb_id') if item_data else 'unknown'}), label '{label}' will apply when added to Plex")
            return False

        # Check if label already exists (avoid duplicate API call)
        existing_labels = [tag.tag for tag in plex_item.labels]
        if label in existing_labels:
            logging.debug(f"Label '{label}' already exists on Plex item {plex_item.title}")
            return True

        # Add label
        plex_item.addLabel(label)
        logging.debug(f"Applied label '{label}' to Plex item '{plex_item.title}'")
        return True

    except Exception as e:
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

    label_config = get_label_config_for_source(content_source)

    if not label_config:
        logging.debug(f"DEBUG determine_labels: No label config found for source {content_source}")
        return []

    labels = []
    label_mode = label_config.get('label_mode', 'requester')  # 'requester', 'fixed', 'list_name'

    logging.info(f"DEBUG determine_labels: source={content_source}, mode={label_mode}, detail={repr(content_source_detail)}")

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

    content_source = item.get('content_source')
    if not content_source:
        logging.debug(f"Item {item_id} ({item_title}) has no content_source, skipping label application")
        return 0

    logging.info(f"apply_labels_for_item called for: {item_title} (ID: {item_id}, source: {content_source})")

    # Debug logging to see what content_source_detail we have
    content_source_detail = item.get('content_source_detail')
    logging.info(f"DEBUG: content_source_detail for item {item_id}: {repr(content_source_detail)}")

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
