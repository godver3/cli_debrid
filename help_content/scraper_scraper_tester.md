### Scraper Tester Help

This page allows you to test the application's scraping process for specific media items using different quality versions and potentially modified settings. It's a powerful tool for fine-tuning scraper behavior and understanding why certain results are chosen or filtered out.

**Note:** Due to the complexity of the interface, the Scraper Tester is **not available on mobile devices**. Please use a desktop browser.

**Workflow:**

The typical workflow involves searching for media, configuring the scrape parameters, running the test, and analyzing the results.

1.  **Search for Media:**
    *   Use the input field at the top to enter the title of a movie or TV show you want to test (e.g., "The Matrix", "Breaking Bad").
    *   Click the `Search` button.
    *   A list of matching media items (usually from TMDB or a similar source) will appear below the search bar.
    *   Click on the desired movie or show from the results list to select it for scraping.

2.  **Configure Scrape Details (Once an item is selected):**
    *   The selected item's title and details will be displayed.
    *   **Version:** Choose the quality/release version profile you want to test from the dropdown menu (e.g., `1080p`, `4K DolbyVision`, `720p`). The settings associated with this version will be loaded.
    *   **TV Show Controls (Only for Episodes):**
        *   If you selected a TV show, dropdowns for `Season` and `Episode` will appear. Select the specific episode you want to test.
        *   `Multi:` Check this box if you want to simulate a search for a season pack or multi-episode torrent instead of a single episode.
    *   **Version Settings:**
        *   `Original Settings:` This panel displays the *current, saved* scraping settings associated with the selected Version profile. These are read-only.
        *   `Modified Settings:` This panel displays an *editable copy* of the settings. You can change values here (e.g., adjust score thresholds, enable/disable filters) to see how they affect the results *without* permanently saving them yet.

3.  **Run the Scrape:**
    *   Click the `Run Scrape` button.
    *   A loading indicator will appear while the application performs two scrapes in the background: one using the `Original Settings` and one using the `Modified Settings`.

4.  **Analyze Results:**
    *   **Scrape Results:**
        *   This section displays the results of both scrapes side-by-side.
        *   `Original Results:` Shows the torrents found and ranked using the *original, unchanged* settings for the selected version.
        *   `Adjusted Results:` Shows the torrents found and ranked using the *settings you potentially modified* in the "Modified Settings" panel.
        *   This comparison makes it easy to see how your setting changes impacted the outcome (e.g., different torrent chosen, more/fewer results filtered).
    *   **Score Breakdown:**
        *   This section (usually appearing below the results) provides a detailed breakdown of how the scoring rules were applied to the torrents found in the **Adjusted Results** scrape. It shows which rules passed/failed and the scores awarded, helping you understand the ranking logic.

5.  **Save or Start Over:**
    *   **Save Modified Settings:** If you are satisfied with the outcome produced by your modified settings, this button (which appears after a scrape) allows you to permanently save the changes you made in the "Modified Settings" panel to the selected Version profile.
    *   **New Search:** Click this button to clear the current test and return to the initial search input field to test a different media item.

This tool is invaluable for understanding and customizing the specifics of the torrent selection process.
