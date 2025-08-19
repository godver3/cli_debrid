
import sqlite3
from datetime import datetime

def manipulate_database(db_path, imdb_id, season_number_to_edit, episode_number_threshold, new_update_date_str):
    """
    Connects to the SQLite database to perform specific data manipulations for testing.
    - Deletes episodes from a specific season of a show.
    - Updates the last refresh date for that show.
    """
    conn = None
    try:
        print(f"Connecting to database at: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        print("Connection successful.")

        # --- 1. Find the item_id for the given imdb_id ---
        print(f"Finding item_id for IMDb ID: {imdb_id}...")
        cursor.execute("SELECT id FROM items WHERE imdb_id = ?", (imdb_id,))
        item_row = cursor.fetchone()
        if not item_row:
            print(f"Error: No item found with IMDb ID {imdb_id}.")
            return
        item_id = item_row[0]
        print(f"Found item_id: {item_id}")

        # --- 2. Find the season_id for the given season number and item_id ---
        print(f"Finding season_id for Season {season_number_to_edit}...")
        cursor.execute("SELECT id FROM seasons WHERE item_id = ? AND season_number = ?", (item_id, season_number_to_edit))
        season_row = cursor.fetchone()
        if not season_row:
            print(f"Error: No season found with number {season_number_to_edit} for item_id {item_id}.")
            return
        season_id = season_row[0]
        print(f"Found season_id: {season_id}")

        # --- 3. Delete episodes with episode_number > threshold ---
        print(f"Deleting episodes from Season {season_number_to_edit} where episode_number > {episode_number_threshold}...")
        cursor.execute(
            "DELETE FROM episodes WHERE season_id = ? AND episode_number > ?",
            (season_id, episode_number_threshold)
        )
        print(f"{cursor.rowcount} episodes deleted.")

        # --- 4. Update the 'updated_at' timestamp for the item ---
        # The 'is_metadata_stale' check uses 'items.updated_at'
        print(f"Updating 'updated_at' for item {imdb_id} to {new_update_date_str}...")
        # The timestamp format in the DB seems to be 'YYYY-MM-DD HH:MM:SS.SSSSSS'
        new_update_date = datetime.strptime(new_update_date_str, '%Y-%m-%d').strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute(
            "UPDATE items SET updated_at = ? WHERE id = ?",
            (new_update_date, item_id)
        )
        print(f"{cursor.rowcount} item's 'updated_at' timestamp updated.")

        # --- Commit changes ---
        conn.commit()
        print("Database changes committed successfully.")

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        if conn:
            conn.rollback()
            print("Transaction rolled back.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        if conn:
            conn.rollback()
            print("Transaction rolled back.")
    finally:
        if conn:
            conn.close()
            print("Database connection closed.")

if __name__ == '__main__':
    # Configuration for the test case
    DATABASE_PATH = '/user/db_content/cli_battery.db'
    IMDB_ID_TO_MODIFY = 'tt4770018'
    SEASON_TO_EDIT = 12
    EPISODE_THRESHOLD = 20
    # Set the 'last refreshed' date to the past to ensure it's stale
    NEW_REFRESH_DATE = '2025-07-01'

    manipulate_database(DATABASE_PATH, IMDB_ID_TO_MODIFY, SEASON_TO_EDIT, EPISODE_THRESHOLD, NEW_REFRESH_DATE)
