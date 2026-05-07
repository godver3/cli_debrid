### TV Show Detail Help

This page displays detailed information about a specific TV show in your library, including artwork, episode tracking, and management options for the entire show, individual seasons, or specific episodes.

**Configuration:**

*   **Artwork Display:** This page requires a **TMDB API key** to display posters and backdrop images.
*   **Ghostlist Mode** Add shows and movies to the ghostlist to prevent them from being re-added. When enabled, deleting content will mark it as ghosted in the database. When disabled, deleted content can be re-added by dynamic content sources. **Note:** Ghostlisting an item also automatically adds it to the Manual Blacklist by IMDb ID, providing an extra layer of protection against re-downloading.
*   **Remove From Content Sources** Controls whether deleted items are removed from your content sources (Trakt lists, Overseerr requests, Plex Watchlist, etc.) during deletion. When enabled, the app will remove items from these sources to prevent them from being automatically re-added, but this makes deletion slower. When disabled, deletion is faster but the item might be re-added if it's still on your lists. **Tip:** Enable this when ghostlist mode is OFF. Disable this when ghostlist mode is ON (since ghostlist already prevents re-addition, and disabling makes deletion much faster).
*   **Clear Artwork Cache** Clears artwork cache if you want to rebuild cache.
*   Configure these in: **Settings → Additional Settings → Library Manager** or **Settings cog icon in Library section**

**Show Information:**

*   **Backdrop:** Large backdrop image displayed behind the content (if available).
*   **Poster:** Show poster image displayed in the header area.
*   **Title & Year:** Show title and original air year.
*   **Metadata:** Displays information such as:
    *   Plot/Synopsis
    *   Rating
    *   Genres
    *   Cast and crew (if available)
    *   Total seasons and episodes

**Action Buttons (Top Right):**

*   **Search:** Opens the scraper to search for this show and find torrents for missing episodes.
*   **Refresh from TMDB:** Fetches updated metadata and episode information from The Movie Database.
*   **Settings:** Opens the show-specific settings modal.
*   **Delete Show / Ghostlist Show:**
    *   This button's behavior depends on your **ghostlist mode setting**.
    *   **If ghostlist mode is enabled:** Shows ghost icon 👻 and "Ghostlist Show". Clicking will soft-delete the entire show (can be recovered later).
    *   **If ghostlist mode is disabled:** Shows trash icon 🗑️ and "Delete Show". Clicking will permanently delete the entire show and all episodes.
    *   A confirmation prompt appears before deletion/ghostlisting.

**Seasons & Episodes:**

The page displays all seasons and their episodes organized in collapsible panels.

*   **Season Panels:**
    *   Click a season header to expand and view episodes.
    *   Each season shows a count of collected episodes (e.g., "Season 1 - 10/10 episodes").
*   **Season Actions:**
    *   **Delete Season 🗑️:** **Always permanently deletes** the entire season and all its episodes, regardless of ghostlist mode setting.
        *   Use this to free up space by removing a specific season you no longer want.
        *   Confirmation prompt: "This will delete all X episodes in Season Y. This action cannot be undone."
    *   **Assign Magnet 🧲:** Opens the Magnet Assign page pre-filled with this show and season (and version). Use this to manually assign a season pack magnet or torrent file.

**Episode Information:**

Each episode in a season displays:

*   **Episode Number & Title:** (e.g., "E01 - Pilot")
*   **Air Date:** When the episode originally aired
*   **Status Indicator:**
    *   ✅ Green check: Episode is collected
    *   🚫 Red icon: Episode is blacklisted
    *   ⏳ Gray icon: Episode is not yet collected
*   **File Details:** If collected, shows:
    *   Filename
    *   Quality version (e.g., 1080p, 4K)
    *   File size
*   **Plot Summary:** Brief description of the episode (click to expand if truncated)

**Episode Actions:**

*   **Search for episode 🔍:** You can manually scrape the episode and select torrent.
*   **Move to wanted <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14m-7-7 7-7 7 7"/></svg>:** moves episode to wanted state to be re-scraped.
*   **Assign Magnet 🧲:** Opens the Magnet Assign page pre-filled with this show, episode, and version. Use this to manually assign a magnet link or torrent file when the scraper can't find one automatically.
*   **Delete Episode 🗑️:** **Always permanently deletes** the specific episode, regardless of ghostlist mode setting.
    *   Confirmation prompt: "Delete [Episode Title]? This action cannot be undone."

**Multiple Files Per Episode:**

If an episode has multiple files (different qualities), you'll see:

*   A list of all files with their quality and size.
*   Options to delete specific files individually.
*   This is useful for removing lower-quality versions while keeping the best one.

**Understanding Delete Behavior:**

The ghostlist mode feature only applies to the main "Delete Show" button at the top of the page. All other delete actions (seasons, episodes, individual files) **always permanently delete** items, regardless of your ghostlist mode setting.

**Why Different Delete Behaviors?**

*   **Ghostlist Entire Show:** You might not want the show anymore, but want the option to recover it later without re-scraping everything.
*   **Permanently Delete Seasons/Episodes:** You're making specific cleanup decisions (e.g., removing a season you've finished, deleting corrupted episodes, removing duplicate files). These should be permanent to free up space immediately.

**Tips:**

*   Expand all season panels to quickly see which episodes are missing across the entire show.
*   Use episode delete buttons to remove corrupted files, then use the download button to re-scrape just that episode.
*   When you have multiple quality versions of an episode, delete the lower quality files to save space.
*   The season episode count (e.g., "10/10") helps you quickly identify incomplete seasons.
*   Use "Refresh from TMDB" if new episodes have been announced or aired but don't appear in your library yet.
*   All delete confirmations clearly state whether the action can be undone or not - read them carefully!

**Common Actions:**

*   **Delete a single corrupted episode:** Find the episode, click Delete Episode, then use the download button to fetch it again.
*   **Remove duplicate quality files:** Expand an episode with multiple files, delete the unwanted quality versions.
*   **Free up space from old seasons:** Click Delete Season on seasons you've watched and no longer need.
*   **Remove entire show you're done with:** Use the main Delete Show button (respects ghostlist setting).
