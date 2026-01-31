### Library Help

This page provides a visual browser for your collected media items. Browse your movies and TV shows with rich artwork, filter by status, and manage your collection with ease.

**Configuration:**

*   **Artwork Display:** This page requires a **TMDB API key** to display posters and backdrop images.
*   **Ghostlist Mode** Add shows and movies to the ghostlist to prevent them from being re-added. When enabled, deleting content will mark it as ghosted in the database. When disabled, deleted content can be re-added by dynamic content sources.
*   **Remove From Content Sources** Controls whether deleted items are removed from your content sources (Trakt lists, Overseerr requests, Plex Watchlist, etc.) during deletion. When enabled, the app will remove items from these sources to prevent them from being automatically re-added, but this makes deletion slower. When disabled, deletion is faster but the item might be re-added if it's still on your lists. **Tip:** Enable this when ghostlist mode is OFF. Disable this when ghostlist mode is ON (since ghostlist already prevents re-addition, and disabling makes deletion much faster).
*   **Clear Artwork Cache** Clears artwork cache if you want to rebuild cache.
*   Configure these in: **Settings → Additional Settings → Library Manager** or **Settings cog icon in Library section**

**View Modes:**

*   **Grid View:** Displays media as poster cards in a grid layout (default).
*   **List View:** Displays media in a compact table format with columns for poster, title, year, status, quality, and size.
*   Click the Grid <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg> or List <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M3 4h18v2H3V4zm0 7h18v2H3v-2zm0 7h18v2H3v-2z"/></svg> buttons to switch between views. Your preference is saved.

**Filters:**

*   **Search:** Type in the search box to find media by title.
*   **Status Filter:**
    *   `All Status`: Shows all items regardless of state.
    *   `Collected`: Shows only fully collected items.
    *   `Missing`: Shows items not yet collected (Wanted or Error states).
    *   `Blacklist`: Shows blacklisted items that won't be automatically downloaded.
    *   `Duplicates`: Shows items with multiple files (useful for managing quality upgrades).
    *   `Upcoming`: Shows unreleased content with future release dates. Items can be sorted by release date (Soonest/Furthest).
    *   `Upgraded`: Shows items that have been marked as upgraded.
*   **Media Type Filter:**
    *   `All Media`: Shows both movies and TV shows.
    *   `Movies`: Shows only movies.
    *   `TV Shows`: Shows only TV shows.
*   **Sort Options:**
    *   `Title (A-Z)` / `Title (Z-A)`: Sort alphabetically by title.
    *   `Year (Newest)` / `Year (Oldest)`: Sort by release year.
    *   `Added (Newest)` / `Added (Oldest)`: Sort by date added to your library.
    *   **Upcoming Filter Only:**
        *   `Release (Soonest)`: Shows items with nearest release dates first.
        *   `Release (Furthest)`: Shows items with most distant release dates first.
        *   Items without release dates always appear at the end.

**Media Cards:**

*   **Poster:** Displays the movie or TV show poster. Hover over the poster to see additional information.
*   **Progress Info (Hover):**
    *   For collected items: Shows completion percentage (e.g., "100% complete" for movies, "75% complete" for shows with some missing episodes or currently airing shows).
    *   For upcoming items: Shows release date (e.g., "Releases Feb 15, 2026") or "Release date TBA" if unknown.
*   **Click Card:** Clicking a card opens the detailed view for that movie or show.

**Selection Mode:**

*   Click the `Select` button to enter selection mode.
*   In selection mode:
    *   Checkboxes appear on each media card (grid view) or as the first column (list view).
    *   Check items you want to perform bulk actions on.
    *   Click `Cancel` to exit selection mode.
*   **Delete Selected:**
    *   After selecting items, click the `Delete` button (shows count of selected items).
    *   A confirmation prompt will appear asking if you want to delete and optionally blacklist the items.
    *   **Note:** Items with ghostlist mode enabled will be ghostlisted (soft-deleted) instead of permanently deleted. Ghostlisted items can be recovered later.

**Action Buttons:**

*   **Refresh <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 1-9 9m9-9a9 9 0 0 0-9-9m9 9h-9m-9 0a9 9 0 0 1 9-9m-9 9a9 9 0 0 0 9 9"/></svg>:** Reloads the library data from the server.
*   **Settings <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v6m0 6v6m-6-6h6m6 0h6"/></svg>:** Opens the Library Manager settings modal for quick access to configuration options.

**Tips:**

*   Use the **Duplicates** filter to find items with multiple files and clean up lower-quality versions.
*   Use the **Upcoming** filter with **Release (Soonest)** sort to see what's coming out soon.
*   When hovering over upcoming items, the overlay shows the release date instead of completion percentage.
*   Grid view is best for browsing visually with posters, while list view is best for quickly scanning details and file sizes.
*   Your view mode (grid/list) preference is saved in your browser and will persist across sessions.
