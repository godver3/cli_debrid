### Queues Help

This page provides a real-time view of the application's background task queues, allowing you to monitor the status of items being processed.

**Initialization Status:**

*   When the application starts or restarts certain processes, an "Initializing system..." message may appear at the top.
*   This section shows the current initialization step and a progress bar indicating the overall progress.
*   If an error occurs during initialization, the status box will turn red and display error details.

**Queue Display:**

*   The main part of the page lists various queues, each representing a different stage in the item processing pipeline.
*   **Expand/Collapse:** Click on a queue title (e.g., "Wanted") to expand or collapse its contents. Your preference for each queue (expanded or collapsed) is saved in your browser.
*   **Item Count:** The total number of items currently in each queue is displayed next to the queue title.
*   **Filename Toggle (`Checking` Queue):** The "Checking" queue title has a file icon (<i class="fas fa-file"></i>). Click this icon to toggle the visibility of the specific filename being checked for items in this queue. This preference is also saved.
*   **Copy Item Details:** Click anywhere on an item's text within a queue to copy its details (title, year, version, etc.) to your clipboard. A "Copied!" tooltip will appear briefly for confirmation.

**Queue Sections:**

*   **`Upgrading`:** Items that are being considered for Upgrading. They have been collected and now for 24 hours cli_debrid will try to find upgrades once an hour.
*   **`Wanted`:** Items identified from your lists that need to be downloaded but haven't been processed by the scraper yet. Shows the time the item was last scraped. If within 24 hours of scraping items will remain in the `Wanted` queue.
*   **`Scraping`:** Items actively being searched for download sources (torrents/magnets).
*   **`Adding`:** Items for which a source has been found and are being added to your debrid service.
*   **`Checking`:** Items currently being monitored for local presence, either in Plex or in your local mount.
    *   **Cached:** Items found in the debrid cache, ready for immediate transfer/linking.
    *   **Downloading:** Items actively being downloaded by the debrid service. Shows a progress bar and the current download state. Multiple files from the same torrent may be grouped under a single progress bar.
    *   Filenames (toggleable) show the specific file being processed.
*   **`Final Check`:** Items for which initial scraping attempts failed. These items wait for a configured delay (defined in settings) before one last search attempt is automatically triggered. Shows when the item entered this waiting period. If the final search is successful, the item moves to `Adding`. If it fails again, the item is typically moved to `Blacklisted`.
*   **`Pending Uncached`:** Items associated with uncached torrents that would take your account over its limit wait in the Pending Uncached queue until your download limit returns to normal.
*   **`Sleeping`:** Items that cli_debrid failed to find. Shows a "Wake Count" indicating how many times the item has been retried.
*   **`Unreleased`:** Items identified but waiting for their release date before processing continues. Displays the relevant release date(s). Items requiring a physical release will indicate this.
*   **`Blacklisted`:** Items that have been explicitly marked to be ignored by the application.

**Hidden Items Summary:**

*   If there are many items in the queues, only a subset might be displayed initially for performance reasons.
*   A summary section at the bottom will indicate how many additional items exist in each queue but are not currently shown on the page.

