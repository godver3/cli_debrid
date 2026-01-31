### Movie Detail Help

This page displays detailed information about a specific movie in your library, including artwork, metadata, files, and management options.

**Configuration:**

*   **Artwork Display:** This page requires a **TMDB API key** to display posters and backdrop images.
*   **Ghostlist Mode** Add shows and movies to the ghostlist to prevent them from being re-added. When enabled, deleting content will mark it as ghosted in the database. When disabled, deleted content can be re-added by dynamic content sources.
*   **Remove From Content Sources** Controls whether deleted items are removed from your content sources (Trakt lists, Overseerr requests, Plex Watchlist, etc.) during deletion. When enabled, the app will remove items from these sources to prevent them from being automatically re-added, but this makes deletion slower. When disabled, deletion is faster but the item might be re-added if it's still on your lists. **Tip:** Enable this when ghostlist mode is OFF. Disable this when ghostlist mode is ON (since ghostlist already prevents re-addition, and disabling makes deletion much faster).
*   **Clear Artwork Cache** Clears artwork cache if you want to rebuild cache.
*   Configure these in: **Settings → Additional Settings → Library Manager** or **Settings cog icon in Library section**

**Movie Information:**

*   **Backdrop:** Large backdrop image displayed behind the content (if available).
*   **Poster:** Movie poster image displayed in the header area.
*   **Title & Year:** Movie title and release year.
*   **Metadata:** Displays information such as:
    *   Plot/Synopsis
    *   Rating
    *   Runtime
    *   Genres
    *   Cast and crew (if available)

**Action Buttons (Top Right):**

*   **Search <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>:** Opens the scraper to search for this movie and find additional torrents.
*   **Refresh from TMDB <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9"/><path d="M21 12a9 9 0 0 1-9 9"/></svg>:** Fetches updated metadata from The Movie Database (TMDB).
*   **Settings <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>:** Opens the movie-specific settings modal.
*   **Delete Movie <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg> / Ghostlist Movie 👻:**
    *   This button's behavior depends on your **ghostlist mode setting** (configured in Settings → Additional Settings → ghostlist mode).
    *   **If ghostlist mode is enabled:** Shows ghost icon 👻 and "Ghostlist Movie". Clicking will **soft-delete** the movie (can be recovered later).
    *   **If ghostlist mode is disabled:** Shows trash icon 🗑️ and "Delete Movie". Clicking will **permanently delete** the movie and all associated files.
    *   A confirmation prompt appears before deletion/ghostlisting.

**Files Section:**

Displays all files associated with this movie.

*   **File Information:**
    *   Filename and path
    *   Quality version (e.g., 1080p, 4K)
    *   File size
    *   Added date
*   **File Actions:**
    *   **Move to Wanted <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14m-7-7 7-7 7 7"/></svg>:** Moves this file back to the wanted queue (useful if you want to search for a better version).
    *   **Delete File 🗑️:** **Always permanently deletes** the individual file, regardless of ghostlist mode setting.
        *   When deleting individual files, the ghostlist mode feature does NOT apply - files are always permanently removed.
        *   This allows you to remove specific files (e.g., delete a 720p version while keeping the 1080p version) without ghostlisting.
*   **Multiple Files:**
    *   If a movie has multiple files (duplicates), all files are listed.
    *   You can delete individual files to keep only your preferred quality.

**Important Notes:**

*   **Top-Level Delete vs. File Delete:**
    *   The main "Delete Movie" button in the top-right respects the ghostlist mode setting.
    *   Individual file "Delete" buttons always permanently delete, regardless of the ghostlist mode setting.
*   **Ghostlist vs. Delete:**
    *   **Ghostlisted items:** Soft-deleted, can be recovered, won't be automatically re-added.
    *   **Deleted items:** Permanently removed from database, files deleted from disk, cannot be recovered (unless re-downloaded).
*   **Confirmation Prompts:**
    *   Deletion actions show clear confirmation messages indicating whether the action can be undone:
        *   "Ghostlisted items can be recovered."
        *   "This action cannot be undone."

**Tips:**

*   If you have multiple quality versions of a movie, use the individual file delete buttons to remove lower-quality versions while keeping the best one.
*   Use "Move to Wanted" to trigger a re-scrape if you want to find a better quality version without fully deleting the movie.
*   The backdrop image creates an immersive viewing experience - if it doesn't appear, check your TMDB API key configuration.
*   Refreshing from TMDB is useful if metadata was recently updated (e.g., new cast members, corrected plot information).
