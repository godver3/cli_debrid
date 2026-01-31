# Database Backup System

## Overview

The application implements an automatic database backup system that creates periodic backups of the `media_items.db` SQLite database. The system is designed to be safe, non-intrusive, and reliable even during active operations.

## Key Features

- **Automatic scheduled backups** every 24 hours during runtime
- **Startup backup** when the application launches
- **Idle-time scheduling** to avoid performance impact
- **Safe concurrent operation** using SQLite's BACKUP API
- **Configurable retention** (default: 3 most recent backups)
- **Automatic cleanup** of old backups and orphaned files
- **Non-blocking operation** that doesn't interrupt normal functionality

## How It Works

### 1. Backup Triggers

The database is backed up in two scenarios:

#### Startup Backup
- Runs immediately when the application starts
- Located in: [main.py:1291](main.py#L1291)
- Creates initial backup before any operations begin

#### Scheduled Backup
- Runs every 24 hours during runtime
- Task name: `task_backup_database`
- Located in: [run_program.py:2845](queues/run_program.py#L2845)
- Only runs when system is idle (see Idle Detection below)

### 2. SQLite BACKUP API Method

The backup uses SQLite's built-in BACKUP API instead of simple file copying. This provides several critical advantages:

**Why SQLite BACKUP API?**
- **Transactionally consistent** - Creates a point-in-time snapshot
- **Non-blocking** - Doesn't lock the database for extended periods
- **Safe during writes** - Works correctly even during active deletions or scraping
- **WAL-mode compatible** - Handles Write-Ahead Logging correctly
- **Database integrity** - Guaranteed consistent backup state

**Technical Implementation:**
```python
# Open source database in read-only mode (minimizes locking)
source_conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=30)

# Create backup database connection
backup_conn = sqlite3.connect(backup_path, timeout=30)

# Perform atomic backup using SQLite's backup() method
source_conn.backup(backup_conn)
```

This method is used by professional database tools and is the recommended approach for SQLite backups.

### 3. Idle Detection

Before running a scheduled backup, the system checks if it's safe to proceed:

**Idle Conditions (all must be true):**
1. **No active tasks executing** - Checks `currently_executing_tasks` is empty
2. **No scraping/adding in progress** - Verifies Scraping and Adding queues are empty
3. **Queue not paused** - Ensures system isn't in maintenance mode

**If system is busy:**
- Backup is skipped with a log message: `System is busy, skipping scheduled backup`
- Will retry at the next scheduled time (24 hours later)
- No performance impact on active operations

**Located in:** [run_program.py:2870](queues/run_program.py#L2870) - `_is_system_idle_for_backup()`

### 4. Backup Process Flow

```
1. Check if system is idle
   ├─ If busy → Skip and log, retry tomorrow
   └─ If idle → Proceed

2. Create backup directory if needed
   └─ Location: /user/db_content/backups/

3. Generate timestamped filename
   └─ Format: media_items_YYYYMMDD_HHMMSS.db
   └─ Example: media_items_20260130_003242.db

4. Open database connections
   ├─ Source: Read-only connection
   └─ Target: New backup file

5. Execute SQLite BACKUP API
   └─ Creates consistent snapshot

6. Verify backup created successfully
   └─ Check file exists and log size

7. Cleanup old backups
   ├─ Sort by modification time
   ├─ Keep 3 most recent
   └─ Delete older backups

8. Remove orphaned files
   └─ Clean up .db-wal and .db-shm files
```

### 5. Backup Location and Naming

**Directory:** `/user/db_content/backups/` (or `$USER_DB_CONTENT/backups/`)

**File Naming Convention:**
- Format: `media_items_YYYYMMDD_HHMMSS.db`
- Timestamp represents when backup was created
- Always sorted by modification time for retention

**Example Backups:**
```
backups/
├── media_items_20260130_003242.db  (539 MB) - Most recent
├── media_items_20260129_002156.db  (537 MB) - Yesterday
└── media_items_20260128_001834.db  (535 MB) - 2 days ago
```

### 6. Retention Policy

**Default:** Keep 3 most recent backups

**How Retention Works:**
1. After creating new backup, list all backup files
2. Sort by modification time (newest first)
3. Keep the first 3 files in the sorted list
4. Delete all remaining files

**Configurable:** The `max_backups` parameter can be changed in `backup_database(max_backups=3)` in [main.py:130](main.py#L130)

### 7. Orphaned File Cleanup

SQLite uses auxiliary files during operation:
- `.db-wal` - Write-Ahead Log file
- `.db-shm` - Shared Memory file

These can be left behind in the backups directory. The backup process automatically cleans these up to prevent clutter.

**Cleanup Process:**
- Scans backups directory for `*.db-wal` and `*.db-shm` files
- Removes any found (they're not part of backup files)
- Logs cleanup: `Cleaned up X orphaned SQLite WAL/SHM file(s)`

## Performance Characteristics

### Expected Backup Times

For a **500-539 MB database:**

| Storage Type | Expected Time | Notes |
|--------------|---------------|-------|
| Modern SSD | 3-10 seconds | Typical for most systems |
| Standard HDD | 10-20 seconds | Depends on disk speed |
| Degraded/Busy Storage | 30-60 seconds | May trigger timeout |
| Network Storage | 5-30 seconds | Depends on network speed |

### Timeout Protection

- **Connection timeout:** 30 seconds
- **If timeout occurs:** Backup fails gracefully and logs error
- **Impact on app:** None - app continues normally
- **Next attempt:** Will retry at next scheduled time

### Resource Usage

- **CPU:** Minimal - SQLite handles backup efficiently
- **Memory:** ~10-20 MB additional during backup
- **Disk I/O:** ~1 GB (500 MB read + 500 MB write)
- **Database locks:** Minimal - read-only connection used

## Safety Features

### 1. Database Locking Prevention

**Problem:** Simple file copy could lock database during heavy writes

**Solution:**
- Read-only source connection
- SQLite BACKUP API handles concurrent operations
- No extended locks on source database

**Result:** Safe to backup even during:
- Active deletions (e.g., deleting 200 items)
- Scraping operations
- Adding new content
- Queue processing

### 2. Consistent State Guarantee

**Problem:** File copy during writes might capture inconsistent state

**Solution:**
- SQLite BACKUP API creates transactional snapshot
- Point-in-time consistency guaranteed
- Either before or during operation, but always valid

**Result:** Backup always contains valid, consistent database

### 3. Graceful Failure Handling

**If backup fails:**
- Error logged with details
- Application continues normally
- Previous backups remain intact
- Will retry at next scheduled time

**Error scenarios handled:**
- Database file not found
- Disk space issues
- Permission problems
- Connection timeouts
- SQLite errors

## Monitoring and Troubleshooting

### Log Messages

**Successful Backup:**
```
[DATABASE_BACKUP] Starting scheduled database backup (system is idle)
Starting SQLite backup from /user/db_content/media_items.db to /user/db_content/backups/media_items_20260130_003242.db
SQLite backup completed successfully
Database backup created: /user/db_content/backups/media_items_20260130_003242.db (539.0 MB)
Cleaned up 2 old backup(s), keeping 3 most recent
Cleaned up 4 orphaned SQLite WAL/SHM file(s)
[DATABASE_BACKUP] Scheduled database backup completed successfully
```

**System Busy (Skipped):**
```
[DATABASE_BACKUP] System busy: Scraping=15, Adding=3
[DATABASE_BACKUP] System is busy, skipping scheduled backup
```

**Backup Failure:**
```
[DATABASE_BACKUP] Error in scheduled backup task: database is locked
SQLite error creating database backup: database is locked
[DATABASE_BACKUP] Scheduled database backup failed
```

### Common Issues

#### Issue: Backup consistently skipped

**Symptoms:** Logs show "System is busy, skipping scheduled backup" every day

**Cause:** System never reaches idle state

**Solutions:**
1. Check queue sizes - may have large backlog
2. Verify scraping/adding queues eventually empty
3. Consider adjusting idle detection thresholds
4. Check for stuck tasks in `currently_executing_tasks`

#### Issue: Backup takes >20 seconds

**Symptoms:** Backup completes but takes unusually long

**Cause:** Slow storage or high I/O contention

**Solutions:**
1. Check disk health and performance
2. Verify no other heavy I/O operations running
3. Consider moving database to faster storage
4. Monitor for disk space issues

#### Issue: Backup timeouts

**Symptoms:** Log shows "SQLite error: database is locked" or timeout errors

**Cause:** Database heavily loaded or storage too slow

**Solutions:**
1. Increase timeout from 30s to 60s in code
2. Check for long-running transactions
3. Verify database not corrupted (`PRAGMA integrity_check`)
4. Consider running backup during off-peak hours

#### Issue: No backups in directory

**Symptoms:** Backups directory empty or missing recent backups

**Cause:** Could be multiple issues

**Solutions:**
1. Check logs for backup task execution
2. Verify task is enabled in `enabled_tasks`
3. Check disk space in backup directory
4. Verify permissions on backup directory
5. Look for error messages in logs

## Manual Backup

While automatic backups run every 24 hours, you can trigger a manual backup:

### Method 1: Restart Application
- Backup runs automatically at startup
- Creates backup in `/user/db_content/backups/`

### Method 2: Call Function Directly (Future Enhancement)
*Manual backup button in UI - not yet implemented*

Could be added to Database page with API endpoint:
```python
@app.route('/database/backup', methods=['POST'])
def manual_backup():
    from main import backup_database
    success = backup_database(max_backups=3)
    return jsonify({'success': success})
```

## Restoring from Backup

If you need to restore from a backup:

### Steps:

1. **Stop the application:**
   ```bash
   supervisorctl stop all
   ```

2. **Navigate to database directory:**
   ```bash
   cd /mnt/data/appdata/cli_debrid/db_content
   ```

3. **Backup current database (just in case):**
   ```bash
   cp media_items.db media_items.db.before_restore
   ```

4. **List available backups:**
   ```bash
   ls -lh backups/media_items_*.db
   ```

5. **Restore from chosen backup:**
   ```bash
   cp backups/media_items_20260130_003242.db media_items.db
   ```

6. **Set correct permissions:**
   ```bash
   chown appuser:appgroup media_items.db
   chmod 644 media_items.db
   ```

7. **Start the application:**
   ```bash
   supervisorctl start all
   ```

8. **Verify restore successful:**
   - Check logs for startup messages
   - Verify data appears correct in UI
   - Check database page shows expected item counts

### Important Notes:

- **Point-in-time restore:** You'll lose any changes made after the backup timestamp
- **WAL files:** No need to restore .db-wal or .db-shm files - they'll be recreated
- **Consistency check:** Run `PRAGMA integrity_check` after restore to verify
- **Content source sync:** May need to re-sync content sources after restore

## Configuration

### Backup Interval

**Current:** 24 hours (daily)

**Location:** [run_program.py:295](queues/run_program.py#L295)
```python
'task_backup_database': 24 * 60 * 60,  # Run every 24 hours (daily backup)
```

**To change:**
- Modify the interval in seconds
- Example for 12 hours: `12 * 60 * 60`
- Example for weekly: `7 * 24 * 60 * 60`

### Backup Retention

**Current:** 3 backups

**Location:** [main.py:130](main.py#L130)
```python
def backup_database(max_backups=3):
```

**To change:**
- Modify `max_backups` parameter
- Higher number = more backups kept, more disk space used
- Lower number = fewer backups, less disk space used

### Idle Detection Thresholds

**Current checks:**
- No executing tasks
- Scraping queue empty
- Adding queue empty
- Queue not paused

**Location:** [run_program.py:2870](queues/run_program.py#L2870)

**To adjust:**
```python
# Allow backup if scraping queue has fewer than 10 items
if scraping_size > 10:  # Changed from > 0
    return False
```

## Technical Details

### Implementation Files

| File | Purpose | Key Functions |
|------|---------|---------------|
| [main.py](main.py#L130-L232) | Core backup logic | `backup_database()` |
| [run_program.py](queues/run_program.py#L2845-L2925) | Scheduled task | `task_backup_database()`, `_is_system_idle_for_backup()` |
| [run_program.py](queues/run_program.py#L248-L295) | Task registration | Task intervals, enabled tasks |

### Dependencies

- **Python sqlite3** - Built-in SQLite library
- **shutil** - Used for old file removal
- **datetime** - Timestamp generation
- **os** - File system operations

### Database Connection Parameters

```python
# Source (read-only to minimize locking)
source_conn = sqlite3.connect(
    f'file:{db_path}?mode=ro',  # Read-only mode
    uri=True,                    # URI filename interpretation
    timeout=30                   # 30 second timeout
)

# Backup (write mode)
backup_conn = sqlite3.connect(
    backup_path,
    timeout=30
)
```

## Best Practices

### For Users

1. **Monitor backup logs** - Check that backups complete successfully
2. **Verify backup schedule** - Ensure backups running at expected times
3. **Check disk space** - Ensure enough space for 3 x 500MB backups (~1.5GB)
4. **Test restore process** - Periodically verify you can restore from backup
5. **Keep backups offsite** - Consider copying backups to external storage

### For Developers

1. **Don't modify during backup** - Avoid schema changes during backup operations
2. **Use transactions** - Wrap large operations in transactions for consistency
3. **Monitor performance** - Watch backup times for degradation
4. **Test failure scenarios** - Verify graceful handling of timeouts/errors
5. **Document changes** - Update this document if modifying backup system

## Future Enhancements

Potential improvements to consider:

1. **Manual backup button** in Database page UI
2. **Compression** of backup files to save disk space
3. **Offsite backup** integration (S3, external storage)
4. **Backup verification** - Integrity check after creation
5. **Configurable retention** in settings UI
6. **Backup notifications** on success/failure
7. **Differential backups** for large databases
8. **Scheduled restore testing** to ensure backups are valid

## Summary

The database backup system provides robust, automatic protection for your media database:

- ✅ **Automatic** - No manual intervention required
- ✅ **Safe** - Won't corrupt database or interrupt operations
- ✅ **Fast** - Typically 5-15 seconds for 500MB database
- ✅ **Reliable** - Uses industry-standard SQLite BACKUP API
- ✅ **Smart** - Only runs when system is idle
- ✅ **Clean** - Automatically manages retention and cleanup

With daily backups and 3-backup retention, you have protection against:
- Accidental deletions
- Database corruption
- Software bugs
- Hardware failures
- User errors

**Remember:** Backups are only useful if you can restore from them. Periodically verify your backups are valid and test the restore process.
