import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any
from .core import get_db_connection
import sqlite3

def create_torrent_tracking_table():
    """Creates the torrent tracking table if it doesn't exist."""
    conn = get_db_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS torrent_additions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_hash TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                trigger_source TEXT NOT NULL,  -- e.g., 'user_manual', 'wanted_upgrade', 'api_request'
                trigger_details TEXT,          -- JSON field with detailed trigger information
                rationale TEXT NOT NULL,       -- Why this torrent was added
                item_data TEXT NOT NULL,       -- JSON field containing complete item data at time of addition
                is_still_present BOOLEAN DEFAULT TRUE,
                removal_reason TEXT,           -- If removed, why it was removed
                removal_timestamp TIMESTAMP,
                additional_metadata TEXT       -- JSON field for any extra tracking data
            )
        ''')
        
        # Create indices for common queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_torrent_hash ON torrent_additions(torrent_hash)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON torrent_additions(timestamp)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trigger_source ON torrent_additions(trigger_source)')
        
        conn.commit()
        logging.info("Successfully created torrent_additions table and indices")
    except sqlite3.Error as e:
        logging.error(f"Error creating torrent tracking table: {e}")
        raise
    finally:
        conn.close()

def record_torrent_addition(
    torrent_hash: str,
    trigger_source: str,
    rationale: str,
    item_data: Dict[str, Any],
    trigger_details: Optional[Dict[str, Any]] = None,
    additional_metadata: Optional[Dict[str, Any]] = None
) -> int:
    """
    Record a new torrent addition to the tracking database.
    
    Args:
        torrent_hash: The hash of the added torrent
        trigger_source: What triggered this addition (e.g., 'user_manual', 'wanted_upgrade')
        rationale: Why this torrent was added
        item_data: Complete item data at time of addition
        trigger_details: Optional detailed information about what triggered the addition
        additional_metadata: Optional additional tracking data
    
    Returns:
        The ID of the newly created record
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            INSERT INTO torrent_additions (
                torrent_hash,
                trigger_source,
                trigger_details,
                rationale,
                item_data,
                additional_metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            torrent_hash,
            trigger_source,
            json.dumps(trigger_details) if trigger_details else None,
            rationale,
            json.dumps(item_data),
            json.dumps(additional_metadata) if additional_metadata else None
        ))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Error recording torrent addition: {e}")
        raise
    finally:
        conn.close()

def mark_torrent_removed(torrent_hash: str, removal_reason: str):
    """
    Mark a torrent as no longer present and record the reason.
    
    Args:
        torrent_hash: The hash of the removed torrent
        removal_reason: Why the torrent was removed
    """
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE torrent_additions
            SET is_still_present = FALSE,
                removal_reason = ?,
                removal_timestamp = CURRENT_TIMESTAMP
            WHERE torrent_hash = ? AND is_still_present = TRUE
        ''', (removal_reason, torrent_hash))
        conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Error marking torrent as removed: {e}")
        raise
    finally:
        conn.close()

def update_cache_check_removal(torrent_hash: str):
    """
    Update the tracking record for a torrent that was removed during cache check.
    This should be called after mark_torrent_removed when a torrent is removed due to not being cached.
    
    Args:
        torrent_hash: The hash of the removed torrent
    """
    return update_torrent_tracking(
        torrent_hash=torrent_hash,
        trigger_source="cache_check",
        rationale="Removed during cache check"
    )

def update_adding_error(torrent_hash: str):
    """
    Update the tracking record for a torrent that failed during the adding process.
    This should be called after mark_torrent_removed when a torrent is removed due to adding errors.
    
    Args:
        torrent_hash: The hash of the removed torrent
    """
    return update_torrent_tracking(
        torrent_hash=torrent_hash,
        trigger_source="adding_error",
        rationale="Failed to add item - see removal reason"
    )

def get_torrent_history(torrent_hash: str) -> list:
    """
    Get the complete history of a specific torrent.
    
    Args:
        torrent_hash: The hash of the torrent to look up
    
    Returns:
        List of all records for this torrent hash
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT * FROM torrent_additions
            WHERE torrent_hash = ?
            ORDER BY timestamp DESC
        ''', (torrent_hash,))
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Error retrieving torrent history: {e}")
        raise
    finally:
        conn.close()

def get_recent_additions(limit: int = 100) -> list:
    """
    Get the most recent torrent additions.
    
    Args:
        limit: Maximum number of records to return
    
    Returns:
        List of recent addition records
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT * FROM torrent_additions
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Error retrieving recent additions: {e}")
        raise
    finally:
        conn.close()

def update_torrent_tracking(
    torrent_hash: str,
    item_data: Dict[str, Any] = None,
    trigger_details: Dict[str, Any] = None,
    additional_metadata: Dict[str, Any] = None,
    trigger_source: str = None,
    rationale: str = None
) -> bool:
    """
    Update an existing torrent tracking record with new metadata.
    
    Args:
        torrent_hash: The hash of the torrent to update
        item_data: Updated item data to merge with existing data
        trigger_details: Updated trigger details to merge with existing data
        additional_metadata: Updated metadata to merge with existing data
        trigger_source: Optional new trigger source to override existing one
        rationale: Optional new rationale to override existing one
        
    Returns:
        bool: True if record was updated, False if no matching record found
    """
    conn = get_db_connection()
    try:
        # First get the existing record
        cursor = conn.execute(
            'SELECT item_data, trigger_details, additional_metadata FROM torrent_additions WHERE torrent_hash = ? ORDER BY timestamp DESC LIMIT 1',
            (torrent_hash,)
        )
        record = cursor.fetchone()
        
        if not record:
            logging.debug(f"No existing record found for torrent hash {torrent_hash}")
            return False
            
        # Merge the new data with existing data
        existing_item_data = json.loads(record['item_data']) if record['item_data'] else {}
        existing_trigger_details = json.loads(record['trigger_details']) if record['trigger_details'] else {}
        existing_additional_metadata = json.loads(record['additional_metadata']) if record['additional_metadata'] else {}
        
        if item_data:
            existing_item_data.update(item_data)
        if trigger_details:
            existing_trigger_details.update(trigger_details)
        if additional_metadata:
            existing_additional_metadata.update(additional_metadata)
            
        # Build the update query and parameters
        update_fields = ['item_data = ?', 'trigger_details = ?', 'additional_metadata = ?']
        params = [
            json.dumps(existing_item_data),
            json.dumps(existing_trigger_details),
            json.dumps(existing_additional_metadata)
        ]
        
        # Add trigger_source and rationale if provided
        if trigger_source:
            update_fields.append('trigger_source = ?')
            params.append(trigger_source)
        if rationale:
            update_fields.append('rationale = ?')
            params.append(rationale)
            
        # Add the torrent hash to params
        params.append(torrent_hash)
        
        # Execute the update
        query = f'''
            UPDATE torrent_additions 
            SET {', '.join(update_fields)}
            WHERE torrent_hash = ?
        '''
        conn.execute(query, params)
        
        conn.commit()
        logging.info(f"Successfully updated tracking record for torrent {torrent_hash}")
        return True
        
    except Exception as e:
        logging.error(f"Error updating torrent tracking record: {e}")
        raise
    finally:
        conn.close() 