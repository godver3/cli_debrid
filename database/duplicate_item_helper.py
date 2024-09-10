import logging
from core import get_db_connection

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def list_media_with_matching_details_different_imdb():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        logging.info("Database connection established")
        
        # Query for movies with different IMDb IDs
        movie_query_imdb = '''
        SELECT m1.id as id1, m1.imdb_id as imdb_id1, m1.title, m1.year, m1.tmdb_id as tmdb_id1,
               m2.id as id2, m2.imdb_id as imdb_id2, m2.tmdb_id as tmdb_id2
        FROM media_items m1
        JOIN media_items m2 ON m1.title = m2.title 
                            AND m1.year = m2.year 
                            AND m1.tmdb_id = m2.tmdb_id
                            AND m1.type = 'movie'
                            AND m2.type = 'movie'
                            AND m1.imdb_id != m2.imdb_id
        WHERE m1.id < m2.id
        '''
        
        # Query for movies with different TMDB IDs
        movie_query_tmdb = '''
        SELECT m1.id as id1, m1.imdb_id as imdb_id1, m1.title, m1.year, m1.tmdb_id as tmdb_id1,
               m2.id as id2, m2.imdb_id as imdb_id2, m2.tmdb_id as tmdb_id2
        FROM media_items m1
        JOIN media_items m2 ON m1.title = m2.title 
                            AND m1.year = m2.year 
                            AND m1.imdb_id = m2.imdb_id
                            AND m1.type = 'movie'
                            AND m2.type = 'movie'
                            AND m1.tmdb_id != m2.tmdb_id
        WHERE m1.id < m2.id
        '''
        
        # Query for episodes with different IMDb IDs
        episode_query_imdb = '''
        SELECT e1.id as id1, e1.imdb_id as imdb_id1, e1.title, e1.year, e1.season_number, e1.episode_number, e1.tmdb_id as tmdb_id1,
               e2.id as id2, e2.imdb_id as imdb_id2, e2.tmdb_id as tmdb_id2
        FROM media_items e1
        JOIN media_items e2 ON e1.title = e2.title 
                            AND e1.year = e2.year 
                            AND e1.season_number = e2.season_number
                            AND e1.episode_number = e2.episode_number
                            AND e1.tmdb_id = e2.tmdb_id
                            AND e1.type = 'episode'
                            AND e2.type = 'episode'
                            AND e1.imdb_id != e2.imdb_id
        WHERE e1.id < e2.id
        '''
        
        # Query for episodes with different TMDB IDs
        episode_query_tmdb = '''
        SELECT e1.id as id1, e1.imdb_id as imdb_id1, e1.title, e1.year, e1.season_number, e1.episode_number, e1.tmdb_id as tmdb_id1,
               e2.id as id2, e2.imdb_id as imdb_id2, e2.tmdb_id as tmdb_id2
        FROM media_items e1
        JOIN media_items e2 ON e1.title = e2.title 
                            AND e1.year = e2.year 
                            AND e1.season_number = e2.season_number
                            AND e1.episode_number = e2.episode_number
                            AND e1.imdb_id = e2.imdb_id
                            AND e1.type = 'episode'
                            AND e2.type = 'episode'
                            AND e1.tmdb_id != e2.tmdb_id
        WHERE e1.id < e2.id
        '''
        
        logging.info("Executing queries")
        cursor.execute(movie_query_imdb)
        movie_results_imdb = cursor.fetchall()
        cursor.execute(movie_query_tmdb)
        movie_results_tmdb = cursor.fetchall()
        cursor.execute(episode_query_imdb)
        episode_results_imdb = cursor.fetchall()
        cursor.execute(episode_query_tmdb)
        episode_results_tmdb = cursor.fetchall()
        
        logging.info(f"Query executed. Results: Movies (IMDb): {len(movie_results_imdb)}, Movies (TMDB): {len(movie_results_tmdb)}, Episodes (IMDb): {len(episode_results_imdb)}, Episodes (TMDB): {len(episode_results_tmdb)}")
        
        matching_media = []
        
        for results, media_type, mismatch_type in [
            (movie_results_imdb, 'movie', 'IMDb ID'),
            (movie_results_tmdb, 'movie', 'TMDB ID'),
            (episode_results_imdb, 'episode', 'IMDb ID'),
            (episode_results_tmdb, 'episode', 'TMDB ID')
        ]:
            for row in results:
                media_info = {
                    'type': media_type,
                    'title': row['title'],
                    'year': row['year'],
                    'mismatch_type': mismatch_type,
                    'item1': {'id': row['id1'], 'imdb_id': row['imdb_id1'], 'tmdb_id': row['tmdb_id1']},
                    'item2': {'id': row['id2'], 'imdb_id': row['imdb_id2'], 'tmdb_id': row['tmdb_id2']}
                }
                if media_type == 'episode':
                    media_info['season_number'] = row['season_number']
                    media_info['episode_number'] = row['episode_number']
                matching_media.append(media_info)
                
                log_message = f"Found matching {media_type} with different {mismatch_type}: {media_info['title']} ({media_info['year']})"
                if media_type == 'episode':
                    log_message += f" S{media_info['season_number']}E{media_info['episode_number']}"
                logging.info(log_message)
                logging.info(f"  Item 1: ID {media_info['item1']['id']}, IMDb ID {media_info['item1']['imdb_id']}, TMDB ID {media_info['item1']['tmdb_id']}")
                logging.info(f"  Item 2: ID {media_info['item2']['id']}, IMDb ID {media_info['item2']['imdb_id']}, TMDB ID {media_info['item2']['tmdb_id']}")
        
        logging.info(f"Total matching media found: {len(matching_media)}")
        print(f"Found {len(matching_media)} media items with matching details but different IMDb or TMDB IDs.")
        return matching_media
    
    except Exception as e:
        logging.error(f"Error listing media with matching details: {str(e)}", exc_info=True)
        return []
    finally:
        conn.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info("Script started")
    results = list_media_with_matching_details_different_imdb()
    if results:
        print("Matching media found:")
        for item in results:
            if item['type'] == 'movie':
                print(f"- Movie: {item['title']} ({item['year']})")
            else:
                print(f"- Episode: {item['title']} ({item['year']}) S{item['season_number']}E{item['episode_number']}")
            print(f"  Item 1: ID {item['item1']['id']}, IMDb ID {item['item1']['imdb_id']}")
            print(f"  Item 2: ID {item['item2']['id']}, IMDb ID {item['item2']['imdb_id']}")
    else:
        print("No matching media found.")
    logging.info("Script finished")