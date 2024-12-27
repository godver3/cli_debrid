import os
import pickle
import logging

class WakeCountManager:
    def __init__(self):
        self.wake_counts = {}
        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        self.file_path = os.path.join(db_content_dir, 'wake_counts.pkl')
        self.load_wake_counts()

    def load_wake_counts(self):
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if os.path.exists(self.file_path):
            with open(self.file_path, 'rb') as f:
                self.wake_counts = pickle.load(f)
            logging.debug(f"Loaded wake counts from {self.file_path}")
        else:
            logging.debug("No existing wake counts file found. Starting with empty wake counts.")

    def save_wake_counts(self):
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with open(self.file_path, 'wb') as f:
            pickle.dump(self.wake_counts, f)
        logging.debug(f"Saved wake counts to {self.file_path}")

    def get_wake_count(self, item_id):
        count = self.wake_counts.get(item_id, 0)
        #logging.debug(f"Retrieved wake count for item {item_id}: {count}")
        return count

    def increment_wake_count(self, item_id):
        old_count = self.wake_counts.get(item_id, 0)
        new_count = old_count + 1
        self.wake_counts[item_id] = new_count
        self.save_wake_counts()
        #logging.debug(f"Incremented wake count for item {item_id}. Old count: {old_count}, New count: {new_count}")
        return new_count

    def set_wake_count(self, item_id, count):
        self.wake_counts[item_id] = count
        self.save_wake_counts()
        #logging.debug(f"Set wake count for item {item_id} to {count}")

wake_count_manager = WakeCountManager()