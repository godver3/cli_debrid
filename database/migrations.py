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

def add_search_performance_indexes():
    """Add indexes to optimize search and status queries (Phase 1.3)"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Index for tmdb_id lookups (critical for live search status checks)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_id
            ON media_items (tmdb_id)
        """)

        # Composite index for tmdb_id + state (optimizes our batch status query)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_state
            ON media_items (tmdb_id, state)
        """)

        # PHASE 1.3 (TV Episodes): Index for imdb_id lookups (critical for TV show queries)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_imdb_id
            ON media_items (imdb_id)
        """)

        # PHASE 1.3 (TV Episodes): Composite index for episode queries
        # Optimizes: WHERE type = 'episode' AND imdb_id = ? AND season_number = ?
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_episode_lookup
            ON media_items (imdb_id, type, season_number, episode_number)
        """)

        # PHASE 1.3 (TV Episodes): Composite index for TMDB episode queries
        # Optimizes: WHERE type = 'episode' AND tmdb_id = ? AND season_number = ?
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_episode_lookup
            ON media_items (tmdb_id, type, season_number, episode_number)
        """)

        conn.commit()
        logging.info("Successfully added search performance indexes including TV episode indexes (Phase 1.3)")

    except Exception as e:
        logging.error(f"Error adding search performance indexes: {str(e)}")
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

def add_statistics_composite_indexes():
    """Add composite indexes to optimize statistics page queries (Phase 1.3)"""
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_upcoming_releases
            ON media_items (type, release_date, state, tmdb_id)
            WHERE type = 'movie' AND release_date IS NOT NULL
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_recently_aired_episodes
            ON media_items (type, release_date, airtime, state, title, season_number, episode_number)
            WHERE type = 'episode' AND release_date IS NOT NULL AND state != 'Blacklisted'
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_calendar_items
            ON media_items (release_date, type, state, tmdb_id, imdb_id)
            WHERE release_date IS NOT NULL
        """)

        conn.commit()
        logging.info("Successfully added statistics composite indexes (Phase 1.3)")

    except Exception as e:
        logging.error(f"Error adding statistics composite indexes: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def add_database_page_indexes():
    """Add indexes to optimize database page filtering and sorting (Phase 1.3)"""
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Individual column indexes for filtering
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_state ON media_items (state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_type ON media_items (type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_content_source ON media_items (content_source) WHERE content_source IS NOT NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_version ON media_items (version) WHERE version IS NOT NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_ghostlisted ON media_items (ghostlisted, state) WHERE ghostlisted = FALSE OR ghostlisted IS NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_title ON media_items (title)")

        # Composite indexes for common filter + sort combinations
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_state_title ON media_items (state, title)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_type_title ON media_items (type, title)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_state_collected_at ON media_items (state, collected_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_items_type_collected_at ON media_items (type, collected_at DESC)")

        conn.commit()
        logging.info("Successfully added database page indexes (Phase 1.3)")

    except Exception as e:
        logging.error(f"Error adding database page indexes: {str(e)}")
        conn.rollback()
    finally:
        conn.close()


def add_library_covering_index():
    """Add covering index for the library page GROUP BY query.

    The library query does:
      WHERE (ghostlisted=0 OR NULL) AND state NOT IN (...) AND type IN (...)
      GROUP BY COALESCE(NULLIF(tmdb_id,''), NULLIF(imdb_id,''), title||year)
      ORDER BY MAX(collected_at) DESC / title / year

    Including all columns referenced in the GROUP BY expression and aggregates
    (tmdb_id, imdb_id, title, year, id, collected_at) allows SQLite to satisfy
    the entire GROUP BY from the index without touching main table rows (index-only scan).
    """
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_covering
            ON media_items (
                type, state, ghostlisted,
                collected_at DESC,
                tmdb_id, imdb_id, title, year, id
            )
        """)
        conn.commit()
        logging.info("Successfully added library covering index")
    except Exception as e:
        logging.error(f"Error adding library covering index: {str(e)}")
        conn.rollback()
    finally:
        conn.close()