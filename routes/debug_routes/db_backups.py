"""Database backups: list, restore, find and delete old databases."""

from flask import jsonify, request
from queues.config_manager import load_config
import logging
from routes import admin_required
import os
from datetime import datetime
import json
from ._common import debug_bp

# ========== Database Backup & Restoration Routes ==========

@debug_bp.route('/api/list_database_backups', methods=['GET'])
@admin_required
def list_database_backups():
    """List all database backups with validation status"""
    try:
        from main import verify_backup

        config = load_config()
        db_content_dir = (config.get('db_content_dir') or os.environ.get('USER_DB_CONTENT', '/user/db_content'))
        backups_dir = os.path.join(db_content_dir, 'backups')

        if not os.path.exists(backups_dir):
            return jsonify({'success': True, 'backups': []})

        backups = []
        for filename in sorted(os.listdir(backups_dir), reverse=True):
            if filename.startswith('media_items_') and filename.endswith('.db'):
                backup_path = os.path.join(backups_dir, filename)

                try:
                    # Get file stats
                    stat_info = os.stat(backup_path)
                    size_mb = round(stat_info.st_size / (1024 * 1024), 2)
                    modified_time = datetime.fromtimestamp(stat_info.st_mtime)
                    age_seconds = (datetime.now() - modified_time).total_seconds()

                    # Calculate age display
                    if age_seconds < 3600:
                        age_display = f"{int(age_seconds / 60)}m ago"
                    elif age_seconds < 86400:
                        age_display = f"{int(age_seconds / 3600)}h ago"
                    else:
                        age_display = f"{int(age_seconds / 86400)}d ago"

                    # Validate backup
                    is_valid = verify_backup(backup_path, min_size_mb=1)

                    # Categorize file (same logic as scan_old_databases)
                    filename_lower = filename.lower()
                    age_days = (datetime.now() - modified_time).days

                    if not is_valid:
                        category = 'corrupted'
                    elif filename.startswith('media_items_pre_restore_'):
                        category = 'safety_backup'
                    elif any(pattern in filename_lower for pattern in ['(copy', '_copy', 'copy)', '_backup', 'backup_']):
                        category = 'manual_copy'
                    elif any(pattern in filename_lower for pattern in ['corrupted', 'old', 'temp', 'backup', 'test', 'main']):
                        category = 'manual_copy'
                    elif filename.startswith('media_items_202'):
                        if age_days > 7:
                            category = 'old_version'
                        else:
                            category = 'recent_backup'
                    else:
                        category = 'unknown'

                    backups.append({
                        'filename': filename,
                        'path': backup_path,
                        'size_mb': size_mb,
                        'modified': modified_time.isoformat(),
                        'age_seconds': int(age_seconds),
                        'age_display': age_display,
                        'category': category,
                        'is_valid': is_valid
                    })

                except Exception as e:
                    logging.error(f"[BACKUP LIST] Error processing {filename}: {e}")
                    continue

        return jsonify({'success': True, 'backups': backups})

    except Exception as e:
        logging.error(f"[BACKUP LIST] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/request_database_restore', methods=['POST'])
@admin_required
def request_database_restore():
    """Create a restore request flag for the next container startup"""
    try:
        from main import verify_backup

        data = request.get_json()
        backup_path = data.get('backup_path')
        create_safety_backup = data.get('create_safety_backup', True)  # Default to True for safety

        # Debug logging
        logging.info(f"[RESTORE REQUEST] Received data: backup_path={backup_path}, create_safety_backup={create_safety_backup}, raw_data={data}")

        if not backup_path:
            return jsonify({'success': False, 'error': 'No backup path provided'}), 400

        if not os.path.exists(backup_path):
            return jsonify({'success': False, 'error': 'Backup file not found'}), 404

        # Verify backup integrity
        if not verify_backup(backup_path, min_size_mb=1):
            return jsonify({'success': False, 'error': 'Backup verification failed - file may be corrupted'}), 400

        # Create restore flag
        config = load_config()
        config_dir = (config.get('config_dir') or os.environ.get('USER_CONFIG', '/user/config'))
        restore_flag_path = os.path.join(config_dir, 'restore_backup.json')

        restore_data = {
            'backup_path': backup_path,
            'create_safety_backup': create_safety_backup,
            'requested_at': datetime.now().isoformat(),
            'requested_by': 'admin'
        }

        with open(restore_flag_path, 'w') as f:
            json.dump(restore_data, f, indent=2)

        logging.info(f"[RESTORE REQUEST] Created restore flag for: {backup_path}")

        # Trigger app restart by scheduling a clean exit
        # Supervisord will automatically restart the app (autorestart=true)
        def delayed_restart():
            import time
            import os
            time.sleep(3)  # Give time for response to be sent
            logging.info("[RESTORE REQUEST] Triggering app restart via clean exit...")
            os._exit(0)  # Clean exit - supervisord will restart the app

        # Start restart in background thread
        import threading
        restart_thread = threading.Thread(target=delayed_restart, daemon=True)
        restart_thread.start()

        return jsonify({
            'success': True,
            'message': 'Restore request created. Container will restart shortly.',
            'backup_path': backup_path
        })

    except Exception as e:
        logging.error(f"[RESTORE REQUEST] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/scan_old_databases', methods=['GET'])
@admin_required
def scan_old_databases():
    """Scan for old database files in both backups and db_content folders"""
    try:
        import sqlite3

        config = load_config()
        db_content_dir = (config.get('db_content_dir') or os.environ.get('USER_DB_CONTENT', '/user/db_content'))
        backups_dir = os.path.join(db_content_dir, 'backups')

        # Current database files that should NEVER be deleted
        protected_files = {'media_items.db', 'users.db', 'cli_battery.db'}

        def is_valid_sqlite_db(filepath):
            """Check if file is a valid SQLite database (any schema)"""
            try:
                if not os.path.exists(filepath):
                    return False

                # Check minimum size (SQLite header is 100 bytes)
                if os.path.getsize(filepath) < 100:
                    return False

                # Check SQLite header
                with open(filepath, 'rb') as f:
                    header = f.read(16)
                    if not header.startswith(b'SQLite format 3\x00'):
                        return False

                # Try to connect and run a simple query
                conn = sqlite3.connect(f'file:{filepath}?mode=ro', uri=True, timeout=5)
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
                cursor.fetchone()
                conn.close()
                return True
            except Exception:
                return False

        def categorize_file(filename, filepath):
            """Categorize a database file"""
            # Check if it's a valid SQLite file (using generic validator, not media_items-specific)
            is_valid = is_valid_sqlite_db(filepath)

            filename_lower = filename.lower()

            # Check for corrupted files (0 bytes or failed validation)
            if not is_valid:
                return 'corrupted'

            # Check for safety backups created before restore
            if filename.startswith('media_items_pre_restore_'):
                return 'safety_backup'

            # Check for manual copies (various naming patterns)
            if any(pattern in filename_lower for pattern in ['(copy', '_copy', 'copy)', '_backup', 'backup_']):
                return 'manual_copy'

            # Check for files explicitly marked as corrupted, old, or temp
            if any(pattern in filename_lower for pattern in ['corrupted', 'old', 'temp', 'backup', 'test', 'main']):
                return 'manual_copy'

            # Check for timestamped backups from today (these are legitimate recent backups)
            if filename.startswith('media_items_202'):
                try:
                    stat_info = os.stat(filepath)
                    age_days = (datetime.now() - datetime.fromtimestamp(stat_info.st_mtime)).days
                    if age_days > 7:  # Older than a week
                        return 'old_version'
                    else:
                        return 'recent_backup'  # Valid recent backup
                except:
                    pass

            return 'unknown'

        def scan_folder(folder_path):
            """Scan a folder for old database files"""
            results = {'files': [], 'total_size_mb': 0}

            if not os.path.exists(folder_path):
                return results

            for filename in os.listdir(folder_path):
                if not filename.endswith('.db'):
                    continue

                # Skip protected files
                if filename in protected_files:
                    continue

                filepath = os.path.join(folder_path, filename)

                try:
                    stat_info = os.stat(filepath)
                    size_mb = round(stat_info.st_size / (1024 * 1024), 2)
                    modified_time = datetime.fromtimestamp(stat_info.st_mtime)
                    age_days = (datetime.now() - modified_time).days

                    # Calculate age display
                    if age_days == 0:
                        age_display = "Today"
                    elif age_days == 1:
                        age_display = "1 day ago"
                    else:
                        age_display = f"{age_days} days ago"

                    category = categorize_file(filename, filepath)

                    results['files'].append({
                        'filename': filename,
                        'path': filepath,
                        'size_mb': size_mb,
                        'age_days': age_days,
                        'age_display': age_display,
                        'category': category
                    })
                    results['total_size_mb'] += size_mb

                except Exception as e:
                    logging.error(f"[SCAN] Error processing {filename}: {e}")
                    continue

            # Also scan for orphaned SQLite temp files (.db-shm, .db-wal)
            # where the parent .db file no longer exists
            for filename in os.listdir(folder_path):
                if not (filename.endswith('.db-shm') or filename.endswith('.db-wal')):
                    continue

                # Check if parent .db file exists
                parent_db = filename.replace('-shm', '').replace('-wal', '')
                parent_path = os.path.join(folder_path, parent_db)

                if not os.path.exists(parent_path):
                    # Orphaned temp file
                    filepath = os.path.join(folder_path, filename)
                    try:
                        stat_info = os.stat(filepath)
                        size_mb = round(stat_info.st_size / (1024 * 1024), 2)
                        modified_time = datetime.fromtimestamp(stat_info.st_mtime)
                        age_days = (datetime.now() - modified_time).days

                        # Calculate age display
                        if age_days == 0:
                            age_display = "Today"
                        elif age_days == 1:
                            age_display = "1 day ago"
                        else:
                            age_display = f"{age_days} days ago"

                        results['files'].append({
                            'filename': filename,
                            'path': filepath,
                            'size_mb': size_mb,
                            'age_days': age_days,
                            'age_display': age_display,
                            'category': 'orphaned_temp'  # New category for orphaned temp files
                        })
                        results['total_size_mb'] += size_mb

                    except Exception as e:
                        logging.error(f"[SCAN] Error processing orphaned temp file {filename}: {e}")
                        continue

            # Sort by age (oldest first)
            results['files'].sort(key=lambda x: x['age_days'], reverse=True)
            return results

        # Scan both folders
        backups_results = scan_folder(backups_dir)
        db_content_results = scan_folder(db_content_dir)

        total_files = len(backups_results['files']) + len(db_content_results['files'])
        total_size = backups_results['total_size_mb'] + db_content_results['total_size_mb']

        return jsonify({
            'success': True,
            'backups_folder': backups_results,
            'db_content_folder': db_content_results,
            'total_files': total_files,
            'total_size_mb': round(total_size, 2)
        })

    except Exception as e:
        logging.error(f"[SCAN] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/delete_old_databases', methods=['POST'])
@admin_required
def delete_old_databases():
    """Delete selected old database files"""
    try:
        data = request.get_json()
        file_paths = data.get('file_paths', [])
        dry_run = data.get('dry_run', False)

        if not file_paths:
            return jsonify({'success': False, 'error': 'No files selected'}), 400

        config = load_config()
        db_content_dir = (config.get('db_content_dir') or os.environ.get('USER_DB_CONTENT', '/user/db_content'))

        # Protected files that should NEVER be deleted
        protected_files = {'media_items.db', 'users.db', 'cli_battery.db'}

        deleted = []
        skipped = []
        total_size_freed = 0

        for filepath in file_paths:
            filename = os.path.basename(filepath)

            # Safety checks
            if filename in protected_files:
                skipped.append({'file': filename, 'reason': 'Protected system file'})
                continue

            if not os.path.exists(filepath):
                skipped.append({'file': filename, 'reason': 'File not found'})
                continue

            # Verify file is in allowed directories
            if not (filepath.startswith(db_content_dir)):
                skipped.append({'file': filename, 'reason': 'File outside allowed directories'})
                continue

            try:
                size_mb = round(os.path.getsize(filepath) / (1024 * 1024), 2)

                if not dry_run:
                    # Delete the main file
                    os.remove(filepath)
                    logging.info(f"[DELETE] Deleted: {filepath} ({size_mb} MB)")

                    # Also delete associated SQLite temporary files (.db-shm, .db-wal)
                    if filepath.endswith('.db'):
                        for ext in ['-shm', '-wal']:
                            temp_file = filepath + ext
                            if os.path.exists(temp_file):
                                try:
                                    os.remove(temp_file)
                                    logging.info(f"[DELETE] Deleted associated file: {temp_file}")
                                except Exception as temp_err:
                                    logging.warning(f"[DELETE] Could not delete {temp_file}: {temp_err}")

                deleted.append(filename)
                total_size_freed += size_mb

            except Exception as e:
                logging.error(f"[DELETE] Error deleting {filepath}: {e}")
                skipped.append({'file': filename, 'reason': str(e)})

        return jsonify({
            'success': True,
            'deleted': deleted,
            'skipped': skipped,
            'deleted_count': len(deleted),
            'skipped_count': len(skipped),
            'total_size_freed_mb': round(total_size_freed, 2),
            'dry_run': dry_run
        })

    except Exception as e:
        logging.error(f"[DELETE] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
