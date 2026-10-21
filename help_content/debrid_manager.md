### Debrid Manager Help

The Debrid Manager is the central hub for monitoring and managing your debrid account directly from cli_debrid. It surfaces everything happening in your Real-Debrid (or AllDebrid) account — active downloads, your full torrent library, a history of what the app has sent to your debrid, and automated maintenance tools for backups and cleanup.

---

**Active Tab**

Shows torrents currently in progress — anything that is actively downloading, queued, errored, or waiting for file selection. The tab badge displays the count of active items. This tab auto-refreshes every 5 seconds while it is open; polling pauses automatically when you switch away and resumes when you return.

*   **Filter bar:** Type any text or a regular expression to filter results by filename or hash in real time.
*   **Status filter:** Narrow the view to a specific category:
    *   **All Status** — Every active item (downloading, queued, etc.)
    *   **Downloading** — Items currently transferring
    *   **Error** — Items in an error state (error, magnet\_error, virus, dead, etc.)
    *   **No Files Selected** — Items added to your debrid account but no files have been selected yet. These will stall indefinitely unless re-inserted or files are manually selected.
*   **Slot badge:** Shows how many active download slots are in use out of your account's maximum.
*   **Sort:** Click any column header to sort by that column. Click again to reverse.
*   **Delete Selected:** Select one or more rows using the checkboxes and click this button to delete those torrents from your debrid account. Requires confirmation.
*   **Select all checkbox (header row):** Selects or deselects all currently visible rows.
*   **Per-row actions:** Each row has a delete icon to remove that individual torrent.
*   **Poll indicator:** The small pulsing dot in the toolbar indicates live auto-refresh is active.

---

**Torrents Tab**

A full paginated view of your entire debrid torrent library — every torrent ever added to your account, not just active ones. Data is fetched from the debrid API on first load and cached locally; subsequent visits use the cache unless you force a refresh.

*   **Filter bar:** Text or regex search against filename and hash.
*   **Type pills:**
    *   **All** — Everything in the library
    *   **Movies** — Items matched to a movie entry in your database
    *   **TV Shows** — Items matched to a TV episode or show entry in your database
    *   **Others** — Items with no database match (manually added torrents, unknown content)
    *   **Dupes** — Filters to items sharing the same torrent hash as another entry in your library. These are true duplicate torrents (same file, added twice).
    *   **Title Match** — Filters to items where multiple torrents share the same resolved title. These may be re-downloads of the same content with different hashes.
    *   **Plex Trash** *(shown when Plex is configured)* — Filters to torrents linked to items currently in the Plex trash (deleted from Plex but not removed from debrid). The ↺ button forces a fresh scan of the Plex trash without waiting for cache expiry.
*   **Sort:** Click any column header (Filename, Size, Status, Added) to sort. The default sort is newest first.
*   **Refresh Library:** Forces a fresh fetch from the debrid API, bypassing the local cache. Use this after making changes in your debrid account from outside cli_debrid.
*   **Delete Selected:** Permanently removes selected torrents from your debrid account. Deleting a torrent here does not remove the corresponding item from the cli_debrid database — it only removes the torrent from debrid.
*   **Delete All Dupes:** Visible only when the Dupes filter is active. Removes all duplicate hash entries, keeping the one with the most links (downloaded files). If no links exist, keeps the entry with the highest download progress.
*   **Pagination:** Results are paginated. Use Prev / Next to navigate. The count display shows your current position and total.
*   **Cross-page selection:** When you select items across multiple pages, a banner appears offering "Select all N matching" to extend the selection to every item matching the current filter — not just the visible page. Use **Reinsert All** from this banner to re-add all selected torrents to debrid (useful for recovering No Files / error states in bulk).
*   **Per-row actions:**
    *   **Info icon** — Opens the Torrent Detail modal showing the full torrent metadata: all files in the torrent, their sizes, selected state, links, and raw debrid status.
    *   **Reinsert icon** — Removes the torrent from debrid and re-adds it by magnet hash. This is the standard fix for "No Files Selected" or stuck torrents. The system automatically selects video files using a size-based heuristic (video files larger than 10% of the largest file).
    *   **Delete icon** — Removes just this torrent from debrid.

---

**Tracker Tab**

A chronological log of every torrent that cli_debrid has ever sent to your debrid account. This is sourced from the app's internal database, not the debrid API, so it persists even after torrents are deleted from debrid.

*   **Filter bar:** Text or regex search against title, hash, and trigger source.
*   **Reload:** Fetches the latest entries from the database.
*   **Columns:**
    *   **Added** — When the torrent was submitted to debrid by cli_debrid.
    *   **Title** — The resolved media title (with year, season, and episode if applicable).
    *   **Trigger** — What caused this torrent to be added. Examples: "scraper", "manual", "upgrading", "wanted".
    *   **Rationale** — A short explanation of why this specific torrent was chosen (e.g. "Best quality match", "Upgrade from 1080p to 4K").
    *   **Status** — Whether the torrent is still present in debrid or has been removed, and when removal occurred if applicable.
    *   **Actions** — Click the info icon to open the Detail modal for that entry.
*   **Detail Modal:** Shows the full item data snapshot captured at the time of submission, including the filled-by filename, version settings used, scraper source, and any additional metadata.

---

**Maintenance Tab**

Configuration and execution of automated backup and cleanup tasks for your debrid account. Settings here control what runs on schedule; actual scheduling is managed separately in the Task Manager (a notice at the top links there directly if scheduling is active).

**Debrid Backup**

Automatically backs up the full list of torrents in your debrid account to JSON files on disk. Three rotating slots are maintained at different ages so you always have recent, medium, and older snapshots available for restore.

*   **Enable toggle:** Turns scheduled backup on or off. When disabled, backups only run when you click Run Now manually.
*   **Backup Retention slots:**
    *   **Daily slot** — The most recent backup. Written on every run. Configurable in hours (default: 24h — meaning the daily slot is replaced each run).
    *   **3-day slot** — Promoted from the daily slot once the daily slot is at least 3 days old (configurable). Preserves a snapshot from roughly 3 days ago.
    *   **7-day slot** — Promoted from the 3-day slot once the 3-day slot is at least 7 days old (configurable). Preserves a snapshot from roughly one week ago.
*   **Save:** Saves retention interval settings.
*   **Run Now:** Triggers a backup immediately regardless of schedule. Promotion to older slots also occurs on each run if the slots are due for rotation.
*   **Backup Slots panel (right column):** Shows the current state of each slot — when it was captured, how many torrents it contains, file size on disk, and a Restore button to re-add all torrents from that snapshot. Restoring skips hashes already present in your account, so it is safe to run against a live account.

**Debrid Cleanup**

Automatically removes unwanted torrents from your debrid account based on configurable rules.

*   **Enable toggle:** Turns scheduled cleanup on or off.
*   **Cleanup Rules (each independently toggleable):**
    *   **Delete errored torrents** — Removes any torrent whose status is error, magnet\_error, virus, or dead. These cannot be downloaded and consume a slot.
    *   **Remove duplicates** — When multiple torrents share the same hash, keeps the best copy and deletes the rest. Selection logic: if any copy has downloaded links, the oldest copy with links is kept; otherwise the copy with the highest download progress is kept.
    *   **Delete stalled torrents** — Removes torrents that have been at 0% progress for longer than the configured number of days. Useful for purging abandoned downloads that were never picked up.
*   **Save:** Saves cleanup rule settings.
*   **Run Now:** Executes cleanup immediately.

**Activity panel (right column):** A live feed of recent backup and cleanup operations, showing timestamps, what action was taken, and outcome (torrents backed up, items deleted, etc.).

---

**Audit Tab**

A set of diagnostic and maintenance tools for auditing the relationship between your cli_debrid database and the Battery metadata database. The tab is split into three sub-panels, toggled by the pill buttons at the top: **Reconcile**, **Symlinks**, and **Battery**.

*   **Reconcile sub-panel:** Identifies mismatches between the torrents in your debrid account and the items recorded in your cli_debrid database.
    *   **Orphaned in debrid** — Torrents present in your debrid account that have no matching entry in the cli_debrid database. These may be manually added torrents or leftover entries from deleted items.
    *   **Missing from debrid** — Items in the cli_debrid database with a Collected state but no corresponding torrent found in your debrid account. These items are effectively broken — the file is gone but the database still thinks it is collected.
    *   **Run Reconcile:** Executes the reconciliation check and populates both lists.
    *   **Per-row actions:** Each row in the orphaned list has a delete icon to remove that torrent from debrid. Each row in the missing list has a re-queue icon to reset the item to a Wanted state so it will be re-downloaded.

*   **Symlinks sub-panel:** Audits the symlink structure used by the symlinked/local mount mode.
    *   **Broken symlinks** — Symlinks that point to a target that no longer exists on disk (e.g., the underlying file was deleted from debrid or moved).
    *   **Orphaned symlinks** — Symlinks with no corresponding entry in the cli_debrid database. These are leftover links from deleted items.
    *   **Run Symlink Audit:** Scans the symlink directory and populates both lists.
    *   **Per-row actions:** Delete icon to remove individual broken or orphaned symlinks from disk.

*   **Battery sub-panel:** Audits the Battery metadata database (cli_battery.db) for consistency issues with your media items.
    *   **Five checks are run:**
        *   **Orphaned Battery Items** — Entries in the Battery database whose IMDB ID does not match any item in the cli_debrid database. These are metadata records for shows or movies that are no longer tracked.
        *   **Missing Battery Items** — Items in the cli_debrid database that have no corresponding metadata record in the Battery database.
        *   **Stale Metadata** — Battery records that have not been refreshed within the expected interval, meaning the metadata may be outdated.
        *   **Orphaned TVDB Mappings** — Entries in the TVDB→IMDB mapping table that reference a TVDB ID with no active items in the database.
        *   **Orphaned TMDB Mappings** — Same as above but for TMDB→IMDB mappings.
    *   **Run Audit:** Executes all five checks and displays the findings.
    *   **Per-row actions (Battery sub-panel):**
        *   **Refresh** — Forces a metadata re-fetch for that item from the upstream API and updates the Battery record.
        *   **Delete** — Removes the Battery record for that item. The item will be re-fetched on the next metadata sync.
        *   **Delete Mapping** — Removes an orphaned TVDB or TMDB mapping record.
        *   **Re-identify** — Two-step process for correcting a wrong IMDB ID on a Battery record. First click **Verify** to confirm the new IMDB ID resolves correctly, then click **Confirm** to commit the change. The old Battery record is deleted, metadata is re-fetched for the new ID, and optionally the IMDB ID on the cli_debrid media item is updated to match.
    *   **Sync Now:** Runs a metadata sync pass (`update_movie_titles()` + `sync_episode_metadata()`) in the background to refresh Battery records for all items in the database.

---

**Usage Tab**

Displays your debrid account's current usage statistics, fetched live from the debrid API.

*   **Account section:**
    *   **Username** — Your debrid account username.
    *   **Premium** — Whether your account is currently active/premium.
    *   **Fidelity Points** *(Real-Debrid)* — Loyalty points accumulated on your account.
*   **Downloads section:**
    *   **Active Slots** — How many of your concurrent download slots are currently in use.
    *   **All-Time Downloaded** — Total data downloaded through your debrid account since it was created.
    *   **Daily Reset In** — Countdown to when your daily download quota resets (midnight UTC for Real-Debrid).
*   **Today's Usage:** A progress bar and text showing how much of today's daily download quota has been used, expressed in GB and as a percentage.
*   **Traffic Details (right column):** A day-by-day breakdown of recent download traffic, showing the date and volume for each day.

---

**Important Notes:**

*   **Deleting from Debrid Manager does not delete from cli_debrid database.** Removing a torrent here only removes it from your debrid account. The corresponding media item in the app database will remain and may be re-queued for download if it is still in a Wanted state.
*   **Reinsert vs Delete + Re-queue:** Reinsert is a fast in-place fix that keeps the existing database state intact. It is the preferred way to unstick a No Files or error torrent without disrupting the app's tracking of that item.
*   **Backup restore is additive.** Restoring from a backup slot only adds torrents that are missing from your current account — it never deletes anything. You can restore safely at any time.
*   **Backup slots use a promotion model.** The daily slot is overwritten on every backup run. The 3-day slot is only updated when the daily slot data is 3+ days old; the 7-day slot is only updated when the 3-day slot data is 7+ days old. This ensures that older slots always contain genuinely older snapshots rather than being overwritten by recent runs.
*   **Scheduled tasks:** Backup and Cleanup can be toggled on in Maintenance, but they also need a corresponding task enabled in the Task Manager (Settings → Task Manager → Features tab) for them to execute on schedule.
*   **Audit tab requires Battery:** The Battery sub-panel in the Audit tab requires the Battery service to be running and its database accessible. If the Battery database is absent or has been deleted, use the Debug Functions page to reinitialise it before running an audit.

---

**Tips:**

*   Use the **Dupes** filter regularly to find and remove hash-identical torrents that accumulate over time from re-scraping the same content.
*   The **Plex Trash** filter is a quick way to identify torrents in debrid that are no longer needed because the linked Plex item has been deleted — clean these up to recover debrid slots.
*   If a torrent shows "No Files Selected" in the Active tab, use the **Reinsert** action on that row — it removes and re-adds the torrent with automatic file selection.
*   Enable both **Backup** and **Cleanup** in Maintenance with scheduling to keep your debrid account tidy and protected automatically.
*   Check the **Tracker** tab to understand exactly what was sent to debrid and why — useful for investigating why a particular file was chosen or why a torrent was re-added unexpectedly.
*   Use the **Audit tab → Battery sub-panel** periodically to catch orphaned metadata records and stale Battery entries — these can cause incorrect titles, missing episode data, or failed scrapes for affected items.
*   The **Reconcile sub-panel** is a good first stop when an item shows as Collected in the database but is not playing — it quickly reveals whether the underlying torrent is still present in debrid.
