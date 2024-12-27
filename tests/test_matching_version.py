import os
import sys
import sqlite3
from datetime import datetime
import logging

# Add the parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.collected_items import add_collected_items
from database.core import get_db_connection

# Set up logging
logging.basicConfig(level=logging.INFO)

def setup_test_db():
    conn = get_db_connection()
    
    # Clear existing test data
    conn.execute('DELETE FROM media_items WHERE imdb_id = ?', ('tt0481499',))
    
    # Insert test movie with a file that doesn't exist
    conn.execute('''
        INSERT INTO media_items (
            imdb_id, title, type, state, version, filled_by_file,
            collected_at, release_date
        ) VALUES (
            'tt0481499', 'The Croods', 'movie', 'Collected', '2160p',
            'The.Croods.2013.2160p.MISSING.mkv', ?, '2013-03-22'
        )
    ''', (datetime.now(),))
    
    conn.commit()
    conn.close()

def test_matching_version_logic():
    # Set up test database
    setup_test_db()
    
    print("\nInitial State:")
    print("-------------")
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT id, state, version, filled_by_file 
        FROM media_items 
        WHERE imdb_id = 'tt0481499'
    ''')
    items = cursor.fetchall()
    for item in items:
        print(f"ID: {item['id']}, State: {item['state']}, Version: {item['version']}, File: {item['filled_by_file']}")
    conn.close()
    
    # Create an empty batch - this simulates no files being found
    test_batch = []
    
    # Process the batch
    add_collected_items(test_batch, recent=False)
    
    # Check the results
    print("\nAfter Processing:")
    print("----------------")
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT id, state, version, filled_by_file 
        FROM media_items 
        WHERE imdb_id = 'tt0481499'
    ''')
    items = cursor.fetchall()
    for item in items:
        print(f"ID: {item['id']}, State: {item['state']}, Version: {item['version']}, File: {item['filled_by_file']}")
    
    conn.close()

if __name__ == '__main__':
    test_matching_version_logic()
