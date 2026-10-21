"""
TMDB API Caching Layer
Provides in-memory and Redis caching for TMDB API responses
"""

import logging
import json
from datetime import datetime, timedelta
from functools import wraps
import time

# In-memory cache (fallback if Redis unavailable)
_memory_cache = {}

# Try to import Redis, but don't fail if unavailable
try:
    import redis
    REDIS_AVAILABLE = True
    try:
        redis_client = redis.Redis(
            host='localhost',
            port=6379,
            db=0,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1
        )
        # Test connection
        redis_client.ping()
        logging.info("✅ Redis connection established for TMDB caching")
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logging.warning(f"⚠️ Redis not available, using in-memory cache only: {e}")
        redis_client = None
        REDIS_AVAILABLE = False
except ImportError:
    logging.warning("⚠️ Redis module not installed, using in-memory cache only")
    redis_client = None
    REDIS_AVAILABLE = False

# Cache TTLs (in seconds)
CACHE_TTL = {
    'trending': 60,          # 1 minute (constantly changing)
    'search': 120,           # 2 minutes
    'details': 300,          # 5 minutes
    'person': 3600,          # 1 hour
    'external_ids': 86400,   # 24 hours
    'db_status': 300,        # 5 minutes (database status checks)
}

def get_cache_key(prefix, *args, **kwargs):
    """Generate a cache key from arguments"""
    key_parts = [prefix]
    key_parts.extend(str(arg) for arg in args)
    key_parts.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
    return ':'.join(key_parts)

def get_from_cache(key):
    """Get data from cache (Redis first, then memory)"""
    # Try Redis first
    if redis_client:
        try:
            cached = redis_client.get(key)
            if cached:
                data = json.loads(cached)
                logging.debug(f"✅ Redis cache HIT: {key}")
                return data
        except Exception as e:
            logging.warning(f"Redis get error: {e}")

    # Fallback to memory cache
    if key in _memory_cache:
        cached_data, expiry = _memory_cache[key]
        if datetime.now() < expiry:
            logging.debug(f"✅ Memory cache HIT: {key}")
            return cached_data
        else:
            # Expired, remove it
            del _memory_cache[key]

    logging.debug(f"❌ Cache MISS: {key}")
    return None

def set_in_cache(key, data, ttl_seconds):
    """Set data in cache (Redis and memory)"""
    # Set in Redis
    if redis_client:
        try:
            redis_client.setex(
                key,
                ttl_seconds,
                json.dumps(data)
            )
            logging.debug(f"✅ Redis cache SET: {key} (TTL: {ttl_seconds}s)")
        except Exception as e:
            logging.warning(f"Redis set error: {e}")

    # Always set in memory cache as backup
    expiry = datetime.now() + timedelta(seconds=ttl_seconds)
    _memory_cache[key] = (data, expiry)
    logging.debug(f"✅ Memory cache SET: {key} (TTL: {ttl_seconds}s)")

    # Clean old memory cache entries (keep max 50 entries - reduced from 100 to prevent memory bloat)
    MAX_CACHE_SIZE = 50
    if len(_memory_cache) > MAX_CACHE_SIZE:
        now = datetime.now()

        # First, remove expired entries
        expired_keys = [k for k, (_, exp) in _memory_cache.items() if exp < now]
        for k in expired_keys:
            del _memory_cache[k]

        # If still over limit after removing expired, evict oldest entries (LRU)
        if len(_memory_cache) > MAX_CACHE_SIZE:
            # Sort by expiry time (oldest first)
            sorted_items = sorted(_memory_cache.items(), key=lambda x: x[1][1])
            entries_to_remove = len(_memory_cache) - MAX_CACHE_SIZE
            for old_key, _ in sorted_items[:entries_to_remove]:
                del _memory_cache[old_key]
            logging.debug(f"Evicted {entries_to_remove} oldest cache entries (LRU)")

def cache_response(cache_type='trending'):
    """Decorator to cache function responses"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = get_cache_key(f'tmdb:{cache_type}', *args, **kwargs)

            # Try to get from cache
            cached_data = get_from_cache(cache_key)
            if cached_data is not None:
                return cached_data

            # Cache miss - call function
            start_time = time.time()
            result = func(*args, **kwargs)
            duration = (time.time() - start_time) * 1000

            logging.info(f"📡 TMDB API call: {func.__name__} took {duration:.0f}ms")

            # Cache the result
            if result:
                ttl = CACHE_TTL.get(cache_type, 60)
                set_in_cache(cache_key, result, ttl)

            return result
        return wrapper
    return decorator

def clear_cache(pattern='*'):
    """Clear cache entries matching pattern"""
    # Clear Redis
    if redis_client:
        try:
            keys = redis_client.keys(f'tmdb:{pattern}')
            if keys:
                redis_client.delete(*keys)
                logging.info(f"🗑️ Cleared {len(keys)} Redis cache entries matching: {pattern}")
        except Exception as e:
            logging.warning(f"Redis clear error: {e}")

    # Clear memory cache
    if pattern == '*':
        _memory_cache.clear()
        logging.info("🗑️ Cleared all memory cache entries")
    else:
        keys_to_delete = [k for k in _memory_cache.keys() if pattern in k]
        for k in keys_to_delete:
            del _memory_cache[k]
        logging.info(f"🗑️ Cleared {len(keys_to_delete)} memory cache entries matching: {pattern}")

def get_cache_stats():
    """Get cache statistics"""
    stats = {
        'redis_available': REDIS_AVAILABLE and redis_client is not None,
        'memory_cache_size': len(_memory_cache),
    }

    if redis_client:
        try:
            info = redis_client.info('stats')
            stats['redis_keys'] = redis_client.dbsize()
            stats['redis_hits'] = info.get('keyspace_hits', 0)
            stats['redis_misses'] = info.get('keyspace_misses', 0)
        except Exception as e:
            logging.warning(f"Redis stats error: {e}")

    return stats

# ========================================
# Database Status Caching
# ========================================

def get_cached_db_statuses(tmdb_ids):
    """
    Cached wrapper for batch database status lookup.

    PHASE 1 FIX #2: Cache database status results to avoid repeated queries
    for the same content across multiple searches.

    Args:
        tmdb_ids: List of TMDB IDs to check

    Returns:
        Dict mapping tmdb_id → status (Collected, Partial, Missing, etc.)
    """
    if not tmdb_ids:
        return {}

    # Generate cache key based on sorted IDs
    cache_key = get_cache_key('db_status', *sorted(tmdb_ids))

    # Try cache first
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        logging.debug(f"✅ DB Status cache HIT for {len(tmdb_ids)} IDs")
        return cached_data

    # Cache miss - import and call the batch function
    from database.database_reading import get_media_items_presence_batch

    start_time = time.time()
    result = get_media_items_presence_batch(tmdb_ids)
    duration = (time.time() - start_time) * 1000

    logging.info(f"📊 DB Status batch query: {len(tmdb_ids)} IDs in {duration:.0f}ms")

    # Cache the result (5 minute TTL)
    set_in_cache(cache_key, result, CACHE_TTL['db_status'])

    return result

def get_cached_episode_info(tmdb_ids):
    """
    Fetch episode information for TV shows with database status.

    Args:
        tmdb_ids: List of TMDB IDs to check (should be TV shows)

    Returns:
        Dict mapping tmdb_id → episode_info dict with counts
    """
    if not tmdb_ids:
        return {}

    from database.core import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        placeholders = ','.join('?' * len(tmdb_ids))
        episode_count_query = f"""
            SELECT
                tmdb_id,
                COUNT(DISTINCT season_number) as distinct_seasons,
                COUNT(DISTINCT season_number || '-' || episode_number) as total_episodes,
                COUNT(DISTINCT CASE WHEN state IN ('Collected', 'Upgrading') THEN season_number || '-' || episode_number END) as collected_episodes,
                COUNT(DISTINCT CASE WHEN state = 'Blacklisted' THEN season_number || '-' || episode_number END) as blacklisted_episodes,
                COUNT(DISTINCT CASE WHEN state = 'Unreleased' THEN season_number || '-' || episode_number END) as unreleased_episodes,
                COUNT(DISTINCT CASE WHEN state IN ('Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping') THEN season_number || '-' || episode_number END) as wanted_episodes
            FROM media_items
            WHERE tmdb_id IN ({placeholders}) AND type = 'episode' AND season_number != 0
            GROUP BY tmdb_id
        """

        cursor.execute(episode_count_query, [str(tid) for tid in tmdb_ids])

        episode_info = {}
        for row in cursor.fetchall():
            episode_info[str(row['tmdb_id'])] = {
                'distinct_seasons': row['distinct_seasons'],
                'total_episodes': row['total_episodes'],
                'collected_episodes': row['collected_episodes'],
                'blacklisted_episodes': row['blacklisted_episodes'],
                'unreleased_episodes': row['unreleased_episodes'],
                'wanted_episodes': row['wanted_episodes']
            }

        return episode_info

    finally:
        cursor.close()
