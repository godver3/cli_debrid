import logging
import os

def check_local_file_for_item(item, source_file_path, original_path_for_symlink):
    logging.info(f"[UPGRADE] Processing confirmed upgrade for {item['title']}")

    # Get the torrent ID of the file being replaced
    old_torrent_id = item.get('upgrading_from_torrent_id')

    # Try removing the old torrent/file
    removal_successful = False
    if old_torrent_id:
        logging.info(f"[UPGRADE] Attempting to remove old torrent {old_torrent_id} via debrid API.")
        try:
            # Assuming remove_torrent returns True on success, False/Exception on failure
            removal_successful = remove_torrent(old_torrent_id, reason="Removed old torrent after successful upgrade")
            if removal_successful:
                logging.info(f"[UPGRADE] Successfully removed old torrent {old_torrent_id} via debrid API.")
            else:
                # Handle cases where remove_torrent might return False without Exception
                logging.warning(f"[UPGRADE] Debrid API call to remove torrent {old_torrent_id} did not confirm success (returned False).")
        except Exception as remove_err:
            logging.error(f"[UPGRADE] Failed to remove old torrent {old_torrent_id} via debrid API: {remove_err}")
    else:
        logging.warning(f"[UPGRADE] Old torrent ID is missing for item {item['id']}. Attempting local file deletion as fallback.")
        if original_path_for_symlink and os.path.exists(original_path_for_symlink):
            try:
                os.remove(original_path_for_symlink)
                removal_successful = True # Assume success if os.remove doesn't raise error
                logging.info(f"[UPGRADE] Successfully removed old local file: {original_path_for_symlink}")
                # Optionally, check if the file is truly gone
                if os.path.exists(original_path_for_symlink):
                    logging.warning(f"[UPGRADE] Local file {original_path_for_symlink} still exists after os.remove attempt.")
                    removal_successful = False
            except OSError as delete_err:
                logging.error(f"[UPGRADE] Failed to delete old local file {original_path_for_symlink}: {delete_err}")
        else:
            logging.warning(f"[UPGRADE] Cannot attempt local file deletion: Path '{original_path_for_symlink}' not found or invalid.")

    # Only proceed with symlink creation if old file removal seemed successful or wasn't applicable
    if removal_successful:
        logging.info("[UPGRADE] Old file/torrent removal successful (or skipped), proceeding with symlink creation.")
        # Create symlink for the new file
        target_path = get_symlink_path(item, source_file_path) # Use the new file path
        if target_path:
            try:
                # ... existing code ...
                logging.info(f"Successfully updated database for upgraded item {item['id']}.")
            except Exception as db_err:
                logging.error(f"Error updating database for upgraded item {item['id']}: {db_err}")
        else:
            logging.error(f"[UPGRADE] Failed to get target path for symlink creation for item {item['id']}.")
    else:
        logging.error(f"[UPGRADE] Failed to remove the old file/torrent for {item['title']}. Skipping symlink creation and database update for the new file to avoid potential issues.")
        # Consider reverting the item state back from 'Checking' if needed?
        # update_media_item_state(item['id'], 'Upgrading') # Example revert

    return removal_successful

def remove_torrent(torrent_id, reason):
    # This function should be implemented to actually remove the torrent
    # It should return True if the removal was successful, False if it wasn't, and raise an exception if there was an error
    pass

def get_symlink_path(item, source_file_path):
    # This function should be implemented to actually get the target path for the symlink
    # It should return the target path if successful, or None if it failed
    pass

def update_media_item_state(item_id, new_state):
    # This function should be implemented to actually update the state of a media item in the database
    pass

# ... rest of the existing code ... 