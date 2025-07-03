### Debug Functions Help

This page provides access to various functions primarily intended for troubleshooting, manual intervention, or advanced maintenance tasks. **Use these functions with extreme caution**, as many of them can lead to data loss or unexpected application behavior if used improperly.

**Important Notes:**

*   **Confirmation Required:** Most actions on this page will trigger a confirmation popup before proceeding. Read the confirmation message carefully.
*   **Loading Indicators:** Actions that take time will display a loading overlay.
*   **Program Status:** Some functions might be disabled or behave differently if the main application background process is not actively running.

**Available Functions:**

*   **Bulk Delete from Database:**
    *   Deletes *all* database entries (movies, episodes) associated with a specific `IMDB ID` or `TMDB ID`.
    *   **Use Case:** Removing all traces of a specific movie or show that was added incorrectly or is causing issues.
    *   **Input:** IMDB ID (e.g., `tt0133093`) or TMDB ID (e.g., `tmdb:603`).
    *   **Caution:** This permanently removes database records.

*   **Download Logs:**
    *   Downloads a specified number of the most recent lines from the main application log file (`debug.log`).
    *   **Use Case:** Providing detailed logs for troubleshooting assistance without needing direct file system access.
    *   **Input:** Number of lines (default 250, max 1000).
    *   **DEPRECATED:** Replaced by `Share Logs` button on `Logs` page.

*   **Delete Database:**
    *   **Highly Destructive:** Deletes the main application database file (`media_items.db`) and associated cache files (`*.cache*.pkl`) from the `db_content` directory.
    *   **Use Case:** Starting completely fresh, resolving severe database corruption.
    *   **Input:** Requires typing `DELETE` into the confirmation box.
    *   **Option:** `Retain blacklisted items`: If checked, the manual blacklist entries will be preserved during deletion.
    *   **Caution:** This action **cannot be undone** and results in complete loss of collected item history, states, etc., unless the blacklist is retained. The application will likely restart or require a manual restart.

*   **Get Collected from Library (Plex):**
    *   Triggers a scan of your configured Plex libraries to update the application's database with items marked as "collected". Only used for Plex libraries (non-symlinked).
    *   **Use Case:** Forcing a refresh of collected status from Plex, potentially after manual library changes or fixing connection issues.
    *   **Input:** `Collection Type`:
        *   `All`: Scan the entire library.
        *   `Recent`: Scan only recently added items (faster).

*   **Get Wanted Content:**
    *   Manually triggers a check for new items from your configured Content Sources (like Trakt lists, Overseerr requests). Note - if you have an existing `Cache` .pkl file for the Content Source it likely won't add anything until you remove the `Cache` file. See `Manage Cache Files`.
    *   **Use Case:** Forcing an immediate check for new wanted items instead of waiting for the scheduled interval.
    *   **Input:** `Source`:
        *   `All Enabled Sources`: Check all sources currently enabled in settings.
        *   Select a specific source to check only that one.

*   **Manual Blacklist:**
    *   Provides a link to the Manual Blacklist page where you can manually add or remove specific `IMDB IDs` or `TMDB IDs` from the application's blacklist, preventing them from being added or processed.

*   **Bulk Queue Actions:**
    *   Allows performing actions (Delete, Move) on multiple items within a specific processing queue.
    *   **Use Case:** Cleaning up stuck items, manually reorganizing items between queues.
    *   **Workflow:**
        1.  Select the `Queue` to view its contents.
        2.  Items in the queue will load; use checkboxes to select items (Shift+Click for range selection, Select/Unselect All buttons available).
        3.  Choose an `Action`: `Delete` or `Move to...`.
        4.  If `Move to...` is selected, choose the `Target Queue`.
        5.  Click `Apply Action` and confirm.
    *   **Caution:** Deleting items removes them from processing; moving items can change their state unexpectedly if done incorrectly.
    *   **DEPRECATED:** Replaced by available actions on `Database` page.

*   **Current Rate Limit State:**
    *   Displays the application's current understanding of API rate limits for tracked external services (like Trakt, Real-Debrid). Shows counts for 5-minute and hourly windows against known limits.
    *   Highlights potential limit breaches in red/orange.
    *   Click `Refresh Rate Limits` to fetch the latest state immediately.
    *   **Use Case:** Diagnosing issues related to being rate-limited by external APIs.

*   **Refresh Release Dates:**
    *   Forces the application to re-fetch release date information for items currently in the `Unreleased` queue.
    *   **Use Case:** Updating release dates if they were initially incorrect or have changed.
    *   **DEPRECATED:** Replaced by `Run Task Manually`.

*   **Send Test Notification:**
    *   Sends a test message to all configured and enabled notification agents (e.g., Discord).
    *   **Use Case:** Verifying that notification settings are correct and reachable.

*   **Move Item ID to Upgrading:**
    *   Manually moves a specific item (identified by its internal database `Item ID`) into the `Upgrading` queue state.
    *   **Use Case:** Forcing upgrading state for a specific item, usually for testing or resolving a failed automatic upgrade. I can assure you that the only one with a use for this is godver3.
    *   **Input:** The internal database ID of the item.

*   **Run Task Manually:**
    *   Allows triggering tasks on demand.
    *   **Use Case:** Executing scheduled tasks (like checking content sources, cleaning up queues) immediately instead of waiting for their schedule.
    *   **Input:** Select the desired task from the dropdown list. Task names are descriptive.

*   **Version Propagator:**
    *   Adds a new "propagated" version entry to the database for every existing item that currently has the "original" version.
    *   **Use Case:** Advanced scenario for if you have a library of 1080p content, and want to add 4k content for each existing item, for example.
    *   **Inputs:**
        *   `Media Type`: Apply to All, Movies Only, or Episodes Only. Recommend caution when applying to Episodes as this can lead to an extremely long `Wanted` queue.
        *   `Original Version`: The existing version profile to find items with.
        *   `Propagated Version`: The *new* version profile to add to those found items.

*   **Symlink Verification Queue:**
    *   Displays statistics and a list of files currently queued for symlink verification (checking if the symlink is present in Plex).
    *   `Refresh Queue`: Updates the displayed stats and list.
    *   `Run Verification Scan`: Manually triggers the background task that performs the verification checks.
    *   **Use Case:** Used to ensure that items successfully scan into Plex when using symlinks.

*   **Convert Library to Symlinks:**
    *   **Advanced/Irreversible:** Initiates a one-time process to convert an existing media library (managed directly by Plex/Emby/Jellyfin) to use the application's symlink structure.
    *   **Use Case:** Migrating a non-symlinked library setup to a symlinked setup managed by this application. **Only run this once** during initial setup/migration. If doing so your steps would be:
        *   Set up cli_debrid with a Plex library (not symlinked)
        *   Complete a full scan of your Plex library
        *   Change the cli_debrid `Required` settings to reflect your symlinked settings
        *   Run this function to create the symlinks
        *   Add the symlinked locations into your Plex instance and complete a full scan (this can take several days)
        *   Remove your existing non-symlinked locations from Plex
        *   You're *done*!
    *   **Caution:** This process modifies how your media is presented and managed. It's not easily reversible. Ensure backups and understand the implications.

*   **Plex Token Status:**
    *   Displays the validity status of saved Plex authentication tokens for configured users. Shows username, validity, expiration, and last checked time.
    *   `Refresh Tokens`: Manually triggers a validation check against Plex for all configured tokens.
    *   **Use Case:** Verifying Plex authentication, diagnosing login issues.

*   **Torrent Tracking:**
    *   Provides a link to a separate page detailing the history of torrents added to the debrid service.

*   **Personal Trakt Token Status:**
    *   Displays the validity and details of the personal Trakt token used for authentication (if configured). Shows status, expiration, last refresh time, and partial token details.
    *   `Refresh Status`: Manually fetches the latest status from Trakt.
    *   **Use Case:** Verifying Trakt authentication.

*   **Direct Emby/Jellyfin Scan:**
    *   Triggers a library scan directly on your configured Emby or Jellyfin server and updates the application's database based on the scan results.
    *   **Use Case:** Forcing a library sync specifically for Emby/Jellyfin users.

*   **Manage Cache Files:**
    *   Lists various cache files (`*.cache*.pkl`) stored in the `db_content` directory.
    *   Allows selecting and deleting specific cache files.
    *   **Use Case:** Clearing potentially stale or corrupted caches to resolve certain issues, often related to content source checks or metadata lookups.

*   **Symlink Library Recovery:**
    *   Provides a link to a separate page designed to help rebuild the application's database by scanning an existing symlink structure.
    *   **Use Case:** Recovering after database loss *if* you were using the symlink feature and the symlink structure is intact. This will take a fair bit of time (one to two hours if starting fresh without metadata in `cli_battery`).

*   **Modify Symlink Base Paths:**
    *   **Advanced:** Allows batch-updating the base directory paths stored in the database for symlink locations and original file locations.
    *   **Use Case:** Required if you move your symlink library directory or the underlying storage directory for original files to a new location *after* they have been added to the database.
    *   **Inputs:** Provide the `Current` base path and the `New` base path for either Symlinks, Originals, or both.
    *   `Dry Run`: Check this box to preview the changes that *would* be made without actually saving them to the database. The preview is shown in a popup.
    *   **Caution:** Incorrect paths will break the application's ability to find files. Use with extreme care and preferably use `Dry Run` first.

*   **Delete Battery Database Files:**
    *   Deletes the internal state database files used by the `cli_battery` background process (`cli_battery.db`, `cli_battery.db-shm`, `cli_battery.db-wal`).
    *   **Use Case:** Resetting the state of the background processor, potentially resolving issues with stuck tasks or internal state corruption. Does *not* delete the main media database.
    *   **Caution:** This resets internal task states and might cause some operations to re-run.
