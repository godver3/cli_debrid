import logging
from .core import get_db_connection

def add_statistics_indexes():
    """Add indexes to optimize statistics queries"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Index for recently added items
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_collected
            ON media_items (
                type, 
                state,
                collected_at DESC
            )
            WHERE collected_at IS NOT NULL
        """)
        
        # Narrow partial index for the statistics page – newest MOVIES
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_recent_movies
            ON media_items(collected_at DESC)
            WHERE type = 'movie'
              AND upgraded = 0
              AND state IN ('Collected', 'Upgrading')
        """)
        
        # Narrow partial index for the statistics page – newest EPISODES (representing shows)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_recent_episodes
            ON media_items(collected_at DESC)
            WHERE type = 'episode'
              AND upgraded = 0
              AND state IN ('Collected', 'Upgrading')
        """)
        
        # Index for recently upgraded items
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_upgraded
            ON media_items (
                upgraded,
                last_updated DESC
            )
            WHERE upgraded = 1 AND last_updated IS NOT NULL
        """)
        
        # Index for collection counts
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_collected_counts
            ON media_items (
                type,
                state,
                imdb_id
            )
            WHERE state = 'Collected'
        """)
        
        conn.commit()
        #logging.info("Successfully added statistics indexes")
        
    except Exception as e:
        logging.error(f"Error adding statistics indexes: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def remove_statistics_indexes():
    """Remove statistics indexes if needed"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        indexes = [
            'idx_media_items_collected',
            'idx_media_items_upgraded',
            'idx_media_items_collected_counts'
        ]
        
        for index in indexes:
            cursor.execute(f"DROP INDEX IF EXISTS {index}")
        
        conn.commit()
        #logging.info("Successfully removed statistics indexes")
        
    except Exception as e:
        logging.error(f"Error removing statistics indexes: {str(e)}")
        conn.rollback()
    finally:
        conn.close() 