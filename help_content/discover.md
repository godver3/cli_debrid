### Discover Help

This page allows you to browse and discover movies and shows using TMDB data, with advanced filtering, curated streaming platform lists, and the ability to save filter presets.

**Requirements:**

*   **TMDB API Key:** Required for all discover functionality. Configure in **Settings > Additional Settings > TMDB**.
*   **MDBList API Key:** Required for MDBList integration. Configure in **Settings > Additional Settings > MDBList**.

**View Modes:**

*   **Search Results:** Shows items matching your search query and active filters.
*   **FlixPatrol Top 10:** Displays top 10 charts from major streaming platforms (Netflix, Disney+, Amazon, HBO, Apple TV+, Paramount+, Hulu, Peacock).
*   **MDBList:** Browse curated movie and TV collections from MDBList (requires MDBList API key).

**Search:**

*   Enter a title, IMDB ID, or TMDB ID in the search box to find content.
*   Results update as you type with a brief debounce delay.
*   Press `Enter` to search or `Esc` to clear.

**Sort Options:**

*   **Sort By:** Popularity, Rating, Vote Count, Release Date, Title or Runtime.
*   **Sort Order:** Click the arrow icon to toggle between ascending and descending order.
*   **Media Type:** Filter by All, Movies, or TV Shows using the buttons below the search bar.

**Filter Panel:**

Click the **Filters** button to open the filter drawer. Filters are organized into collapsible sections:

*   **Date Filters:**
    *   `Year From / To`: Set a release year range.
    *   `Released Within`: Show content released in the last X days.
    *   `Upcoming`: Show content releasing in the next X days.

*   **Ratings & Votes:**
    *   `TMDB Rating`: Set minimum and maximum rating (0-10) using sliders or input fields.
    *   `TMDB Votes`: Set minimum vote count threshold.

*   **Genres:**
    *   Select genres to include or exclude from results.
    *   Use the dropdown to search and multi-select genres.
    *   Selected genres appear as chips with remove buttons.

*   **Keywords:**
    *   Filter by TMDB keywords (e.g., "based on novel", "superhero").
    *   Search for keywords and add them as include or exclude filters.

    **Title Filter:**
    *   Client-side filter to further refine results by title.
    *   Supports plain text matching or regex patterns for advanced filtering.

    **Video Filter:**
    *   Filter for video content that is not classified as standard movies or TV shows.

*   **Languages & Countries:**
    *   Filter by original language or country of origin.
    *   Supports include/exclude for precise filtering.

*   **Providers & Networks:**
    *   `Watch Providers`: Filter by streaming services (requires Watch Region selection).
    *   `Networks`: Filter TV shows by network (e.g., HBO, Netflix, BBC).

*   **Companies:**
    *   Filter by production company (e.g., Marvel Studios, A24).

*   **Runtime:**
    *   Set minimum and maximum runtime in minutes.

**Active Filters:**

*   Applied filters appear as chips at the top of the filter panel.
*   Click the `x` on any chip to remove that filter.
*   Click **Clear Filters** to reset all filters at once.

**Lists (FlixPatrol & MDBList):**

*   **FlixPatrol Top 10:** Select streaming platforms to see their current top 10 charts. Multiple platforms can be selected simultaneously.
*   **MDBList:** Browse curated lists from MDBList (requires MDBList API key).
*   Selected lists appear as chips and can be combined with compatible filters.
*   **Note:** Some filters (Keywords, Cast, Crew, Providers, Networks) are disabled when viewing lists, as they require TMDB discover API parameters not available for list results.

**Presets:**

*   **Save Preset:** Configure your desired filters, then click **Save Preset** and enter a name. Your filter configuration is saved for quick access.
*   **Load Preset:** Click a saved preset to instantly apply its filters.
*   **Manage Presets:** Rename or delete presets from the preset menu.
*   Presets save all filter settings including sort order, genres, ratings, lists, and more.

**Filter Persistence:**

*   Your filter settings are automatically saved to your browser's local storage.
*   When you navigate away (e.g., to view item details) and return, your filters are restored.
*   Filters persist across browser sessions until you explicitly clear them.

**Item Cards:**

*   **Poster:** Displays the movie or show poster from TMDB.
*   **Status Badge (Top Left):** Shows library status with color and icon:
    *   **Green (Archive Box Icon):** Item is fully collected in your library
    *   **Yellow (Partial File Icon):** Item is wanted or partially collected (some episodes for TV shows)
    *   **Blue (Upload Arrow Icon):** Item is being upgraded to better quality
    *   **Dark Gray (Ban Icon):** Item is blacklisted and won't be automatically downloaded
    *   **Orange (Calendar X Icon):** Item is unreleased (future air date)
    *   **Red (X Icon):** Item is missing from your library
    *   **Split Badge (Half & Half):** For TV shows with mixed episode states - left half shows primary state, right half shows secondary state (e.g., yellow/green = some episodes wanted, some collected)
*   **Media Type Badge (Top Right):** Film icon for movies, TV icon for shows
*   **Bottom Info Bar:** Shows rating, release date, and year
*   **Click Card:** Opens the detail view for that item
*   **Add Button (+):** Click to add the item to your wanted list with version selection

**Adding Content:**

*   Click the add button on any item to open the version selection modal.
*   **Movies:** Select quality version(s) and click **Request**.
*   **TV Shows:** Select version(s), then choose "All Seasons" or specific seasons before requesting.

**Adaptive Lists Integration:**

*   Discover filters can be used to create Adaptive Lists in **Settings > Content Sources**.
*   Adaptive Lists automatically populate based on your filter rules (e.g., "TMDB 7+, Action genre, released in last 30 days").
*   Lists from FlixPatrol and MDBList can be used as Adaptive List sources with additional filtering applied.

**Tips:**

*   Use **Released Within** combined with **Rating** filters to find recent well-reviewed content.
*   Combine **FlixPatrol** lists with **Genre Exclude** to filter out unwanted categories from trending charts.
*   Save frequently used filter combinations as **Presets** for one-click access.
*   Use **Keywords** for niche filtering (e.g., "time travel", "alien invasion", "heist").
*   Use **Title Filter** with regex patterns for advanced title matching (e.g., exclude remakes or specific editions).
*   The **Random** sort option is useful for discovering content you might have missed.
