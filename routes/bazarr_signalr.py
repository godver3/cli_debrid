"""
Bazarr SignalR Manager - Real-time event broadcasting for Bazarr integration.

This module manages SignalR connections and broadcasts events when media is
collected, allowing Bazarr to receive real-time notifications about new content.

Event sequence matches CineSync implementation:
- For movies: movie(added) -> delay -> movie(updated)
- For episodes: series(added) -> 3s delay -> series(updated) -> 2s delay -> episodeFile(added)
"""

import logging
import threading
import time
from typing import Dict, List, Any, Optional
from datetime import datetime
from queue import Queue, Empty

# Delay constants (matching CineSync)
SERIES_ADDED_TO_UPDATED_DELAY = 3.0  # 3 seconds between series added and updated
SERIES_UPDATED_TO_EPISODE_DELAY = 2.0  # 2 seconds between series updated and episodeFile
MOVIE_ADDED_TO_UPDATED_DELAY = 1.0  # 1 second between movie added and updated

# Global event queues for SSE connections
_event_queues: Dict[str, Queue] = {}
_queue_lock = threading.Lock()


def register_connection(connection_id: str) -> Queue:
    """Register a new SignalR connection and return its event queue."""
    with _queue_lock:
        if connection_id not in _event_queues:
            _event_queues[connection_id] = Queue(maxsize=100)
            logging.info(f"[SignalR] Registered connection: {connection_id}")
        return _event_queues[connection_id]


def unregister_connection(connection_id: str):
    """Unregister a SignalR connection."""
    with _queue_lock:
        if connection_id in _event_queues:
            del _event_queues[connection_id]
            logging.info(f"[SignalR] Unregistered connection: {connection_id}")


def broadcast_event(event_name: str, event_body: Dict[str, Any]):
    """
    Broadcast an event to all connected SignalR clients.

    Args:
        event_name: Name of the event (e.g., 'movie', 'series', 'episodeFile')
        event_body: Event payload containing resource and action
    """
    from utilities.settings import get_setting

    # Only broadcast if Bazarr integration is enabled
    if not get_setting('Bazarr Integration', 'enabled', False):
        return

    message = {
        'type': 1,  # SignalR message type for method invocation
        'target': 'receiveMessage',
        'arguments': [{
            'name': event_name,
            'body': event_body
        }]
    }

    with _queue_lock:
        dead_connections = []
        for connection_id, queue in _event_queues.items():
            try:
                # Non-blocking put with timeout
                queue.put_nowait(message)
            except Exception:
                # Queue is full, mark for removal
                dead_connections.append(connection_id)

        # Clean up dead connections
        for conn_id in dead_connections:
            del _event_queues[conn_id]

    if _event_queues:
        logging.debug(f"[SignalR] Broadcast '{event_name}' to {len(_event_queues)} connections")


def broadcast_movie_added(movie_resource: Dict[str, Any]):
    """Broadcast a movie added event.

    For new items, we send 'added' first to notify Bazarr of the new resource,
    then 'updated' after a delay to trigger file scanning.

    Runs in a background thread to avoid blocking the main request.
    """
    def _broadcast_sequence():
        # Send 'added' action first for new items
        broadcast_event('movie', {
            'resource': movie_resource,
            'action': 'added'
        })

        # Wait before sending updated (allows Bazarr to process added event)
        time.sleep(MOVIE_ADDED_TO_UPDATED_DELAY)

        # Send 'updated' to trigger file scanning
        broadcast_event('movie', {
            'resource': movie_resource,
            'action': 'updated'
        })

    # Run in background thread to not block the caller
    thread = threading.Thread(target=_broadcast_sequence, daemon=True)
    thread.start()


def broadcast_series_added(series_resource: Dict[str, Any]):
    """Broadcast a series added event for new series."""
    broadcast_event('series', {
        'resource': series_resource,
        'action': 'added'
    })


def broadcast_series_updated(series_resource: Dict[str, Any]):
    """Broadcast a series updated event."""
    broadcast_event('series', {
        'resource': series_resource,
        'action': 'updated'
    })


def broadcast_episode_file_added(
    episode_file_resource: Dict[str, Any],
    series_resource: Optional[Dict[str, Any]] = None
):
    """Broadcast an episode file added event with full sequence.

    Matches CineSync's event sequence:
    1. series(added) with hasFile: false
    2. Wait 3 seconds
    3. series(updated)
    4. Wait 2 seconds
    5. episodeFile(added)
    6. episodeFile(updated)

    Runs in a background thread to avoid blocking the main request.

    Args:
        episode_file_resource: The episode file resource to broadcast
        series_resource: Optional series resource for series events
    """
    def _broadcast_sequence():
        # If we have series info, send series events first
        if series_resource:
            # Create a copy with hasFile: false for the added event
            series_for_added = series_resource.copy()
            if 'statistics' in series_for_added:
                stats = series_for_added['statistics'].copy()
                stats['episodeFileCount'] = 0
                stats['percentOfEpisodes'] = 0.0
                series_for_added['statistics'] = stats

            # 1. Send series 'added' event
            broadcast_event('series', {
                'resource': series_for_added,
                'action': 'added'
            })

            # 2. Wait 3 seconds
            time.sleep(SERIES_ADDED_TO_UPDATED_DELAY)

            # 3. Send series 'updated' event
            broadcast_event('series', {
                'resource': series_resource,
                'action': 'updated'
            })

            # 4. Wait 2 seconds
            time.sleep(SERIES_UPDATED_TO_EPISODE_DELAY)

        # 5. Send episodeFile 'added' event
        broadcast_event('episodeFile', {
            'resource': episode_file_resource,
            'action': 'added'
        })

        # Small delay before updated
        time.sleep(0.5)

        # 6. Send episodeFile 'updated' event to trigger file scanning
        broadcast_event('episodeFile', {
            'resource': episode_file_resource,
            'action': 'updated'
        })

    # Run in background thread to not block the caller
    thread = threading.Thread(target=_broadcast_sequence, daemon=True)
    thread.start()


def get_pending_events(connection_id: str, timeout: float = 0.1) -> List[Dict]:
    """
    Get pending events for a connection.

    Args:
        connection_id: The connection ID
        timeout: How long to wait for events (seconds)

    Returns:
        List of pending event messages
    """
    with _queue_lock:
        queue = _event_queues.get(connection_id)
        if not queue:
            return []

    events = []
    try:
        # Get first event with timeout
        event = queue.get(timeout=timeout)
        events.append(event)

        # Get any additional events without waiting
        while True:
            try:
                event = queue.get_nowait()
                events.append(event)
            except Empty:
                break
    except Empty:
        pass

    return events


def notify_media_collected(media_item: Dict[str, Any], media_type: str):
    """
    Notify Bazarr that media has been collected.

    This function should be called from add_collected_items() when new
    media is successfully collected.

    Args:
        media_item: The collected media item from the database
        media_type: Either 'movie' or 'episode'
    """
    from utilities.settings import get_setting

    # Only notify if Bazarr integration is enabled
    if not get_setting('Bazarr Integration', 'enabled', False):
        return

    try:
        if media_type == 'movie':
            # Import here to avoid circular imports
            from routes.bazarr_spoofing_routes import create_movie_resource
            resource = create_movie_resource(media_item)
            broadcast_movie_added(resource)
            logging.info(f"[SignalR] Notified Bazarr of new movie: {media_item.get('title')}")

        elif media_type == 'episode':
            from routes.bazarr_spoofing_routes import (
                create_episode_file_resource,
                create_series_resource,
                get_episodes_for_series,
                normalize_show_id,
            )
            # Get series info for the full event sequence — same show_id
            # contract as HTTP /api/v3/series (imdb preferred, blank ignored)
            show_id = normalize_show_id(
                media_item.get('imdb_id'), media_item.get('tmdb_id')
            )

            # Build series resource for series events
            series_info = {
                'show_id': show_id,
                'imdb_id': media_item.get('imdb_id'),
                'tmdb_id': media_item.get('tmdb_id'),
                'title': media_item.get('title'),
                'year': media_item.get('year'),
                'genres': media_item.get('genres'),
                'runtime': media_item.get('runtime')
            }

            # Get all episodes for this series to build complete series resource
            try:
                episodes = get_episodes_for_series(show_id) if show_id else [media_item]
            except Exception:
                episodes = [media_item]  # Fall back to just this episode

            series_resource = create_series_resource(series_info, episodes)

            # MUST reuse series_resource['id'] — never re-hash separately.
            # HTTP create_series_resource uses TMDB as id when present; a
            # separate generate_unique_id caused Bazarr FK failures.
            series_id = series_resource['id']
            episode_file_resource = create_episode_file_resource(media_item, series_id)

            # Broadcast with full sequence (series events -> episode events)
            broadcast_episode_file_added(episode_file_resource, series_resource)

            logging.info(
                f"[SignalR] Notified Bazarr of new episode: {media_item.get('title')} "
                f"S{media_item.get('season_number', 0):02d}E{media_item.get('episode_number', 0):02d}"
            )

    except Exception as e:
        logging.error(f"[SignalR] Error notifying Bazarr: {e}")


def get_connection_count() -> int:
    """Get the number of active SignalR connections."""
    with _queue_lock:
        return len(_event_queues)
