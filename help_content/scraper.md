### Scraper Help

This page allows you to search for movies and TV shows, view trending items, and initiate download requests.

**Search Bar:**

*   **Search Term:** Enter the name of the movie or TV show you want to find.
*   **Version Select:** Choose a specific quality profile/version (e.g., 1080p, 4K) to search for. Select "No Version" if you don't have a preference or want to see all available options during the request.
*   **Search Button:** Click to initiate the search based on your term and selected version.

**Trending Movies & Shows:**

*   Displays horizontally scrollable lists of currently popular movies and TV shows based on TMDB data.
*   **Navigation:** Use the left/right arrow buttons (< >) next to the "Trending Movies" and "Trending Shows" titles to scroll through the lists.
*   **Posters & Information:**
    *   If your TMDB API key is configured in Settings, posters will be displayed. Otherwise, placeholders or basic text will be shown. Hovering over a poster often reveals more information like the title and year.
    *   A small colored dot (pip) may appear in the top-right corner of a poster:
        *   <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background-color:#4CAF50; margin-right: 3px; vertical-align: middle;"></span> **Green:** Item is already marked as collected in your library.
        *   <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background-color:#f44336; margin-right: 3px; vertical-align: middle;"></span> **Red:** At least one item of this media content is blacklisted.
        *   <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background-color:#f39c12; margin-right: 3px; vertical-align: middle;"></span> **Orange/Yellow:** Item partially collected.
    *   These dots will be reworked in the future for additional details.
*   **Request Icon (<i class="fas fa-plus"></i>):** A green triangle with a plus sign appears on posters. Clicking this icon initiates the request process for that item (see Requesting Media below).
*   **Tester Icon (<i class="fas fa-cog"></i>):** A green triangle with a gear sign appears on posters. Clicking this icon moves the item into the Scraper Tester module as a shortcut.

**Search Results:**

*   After performing a search, results matching your query are displayed in a grid below the trending sections.
*   Results show posters (if TMDB key is set) and basic information. Hover over a result for more details.
*   Each search result also features the **DB Status Pip** and the **Request Icon (<i class="fas fa-plus"></i>)**, functioning the same way as in the Trending sections.

**Scraping Media:**


*   Clicking on a movie's poster, or an episode card, will perform a scrape for that item. A list of torrents will be provided with size, score, and cache status.
*   You can then either add the item to your account, or manually Assign the item.

**Requesting Media:**

*   Clicking the **Request Icon (<i class="fas fa-plus"></i>)** on any movie or show (in Trending or Search Results) opens the **Version Selection Modal**.
*   **Version Selection Modal:**
    *   This popup lists all the available quality versions configured in the application settings.
    *   **Movies:** Check the box(es) next to the version(s) you want to request.
    *   **Shows:**
        *   First, check the box(es) next to the desired quality version(s).
        *   Then, select whether you want to request "All Seasons" or "Specific Seasons".
        *   If "Specific Seasons" is chosen, a list of available seasons will appear; check the box(es) next to the seasons you want.
    *   Click the **Request** button to submit your request with the selected versions (and seasons for shows).
    *   Click **Cancel** to close the modal without requesting.

**Season/Episode View (TV Shows):**

*   Provides options specifically for TV shows, potentially including:
    *   **Season Dropdown:** Select a specific season to view its episodes.
    *   **Season Pack Button:** *(Functionality inferred)* Likely initiates a request for all episodes of the currently selected season using a default or pre-selected version.
    *   **Request Season Button:** *(Functionality inferred)* Likely opens the Version Selection Modal pre-configured to request the currently selected season(s).

**Other Notes:**

*   **TMDB API Key:** A The Movie Database (TMDB) API key is required to display posters and fetch trending data. You can configure this in the application settings.
