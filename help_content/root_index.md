### Home / Statistics Help

This page provides an overview of your collection, recent activity, and upcoming items based on your Trakt lists and local collection.

**Toggles (Top Left):**

*   **Timezone:** Displays the timezone used for all date/time information on this page.
*   **24h Toggle:** Switch between 12-hour (AM/PM) and 24-hour time formats for displayed times. This preference is saved.
*   **Compact Toggle:** Switch between the default view (with posters) and a more compact, text-based view for the "Recently Added" and "Recently Upgraded" sections. This preference is also saved.

**Main Statistics Sections:**

*   **Collection Stats:** Shows totals for movies, shows, and episodes in your tracked collection, your current active downloads vs. your limit, daily API usage (e.g., Trakt), and application uptime.
    *   *Note:* Active Downloads and Daily Usage change color (Warning/Critical) based on thresholds.
*   **Recently Aired:** Shows episodes from your tracked shows that have aired recently.
    *   Items marked in red (<font color="red">like this</font>) are not yet marked as collected in your library.
    *   Clicking an item opens a selector with links to view it on Trakt, TMDB, and IMDB.
    *   Collected items have a <i class="fas fa-redo"></i> (Rescrape) button to move the item back to a "Wanted" state if needed (e.g., if the file was deleted or incorrect). This does not impact the original item.
*   **Airing Soon:** Lists episodes scheduled to air in the near future. Clickable for external links.
*   **Upcoming Releases:** Shows movies or show premieres expected soon based on your lists. Clickable for external links.
*   **Recently Added Movies/Shows:** Displays the most recent movies and shows added to your collection via cli_debrid. Shows poster images in default view, or just file details in compact view.
*   **Recently Upgraded:** Shows items that were recently replaced with a different version (e.g., higher quality). Displays details about the old and new files/versions.

**Other Notes:**

*   **External Links:** Clicking on items in the "Recently Aired", "Airing Soon", or "Upcoming Releases" lists will open a pop-up allowing you to easily navigate to the item's page on Trakt, TMDB, or IMDB.
*   **Auto-Refresh:** The statistics displayed in the top "Collection Stats" section refresh periodically in the background.
*   **Posters:** If posters are not loading in the "Recently Added/Upgraded" sections, ensure your TMDB API key is set correctly in the application settings. 

---

### Header Bar

The header bar appears at the top of every page and provides navigation, status information, and quick access controls.

*   **Logo/Title:** Clicking the application logo or title ("cli_debrid") always takes you back to the Home page.
*   **Version & Update Indicator:**
    *   Displays the currently running application version (e.g., `v0.5.0m`). The suffix `m` indicates the main branch, while `d` indicates the dev branch.
    *   If an update is available, a download icon (<i class="fas fa-download"></i>) will appear next to the version number. Clicking this icon opens the project's GitHub repository in a new tab.
*   **User Info & Support:**
    *   If the user system is enabled, it displays a "Welcome, [username]" message.
    *   Provides quick links to support the project via GitHub Sponsors (<i class="fab fa-github"></i>), Patreon (<i class="fab fa-patreon"></i>), and Ko-fi (<i class="fas fa-mug-hot"></i>).
*   **Hamburger Menu (Mobile/Tablet):** On smaller screens, a hamburger icon (<i class="fas fa-bars"></i>) appears. Clicking it toggles the main navigation menu.
*   **Navigation Menu:** Contains links to all major sections of the application, grouped by category (Main, System, Tools, Users). On smaller screens, group titles can be clicked to expand their respective sections.
*   **Action Controls:**
    *   **Logout (<i class="fas fa-sign-out-alt"></i>):** (Requires user system) Logs the current user out.
    *   **Start/Stop Program (<i class="fas fa-play"></i> / <i class="fas fa-stop"></i>):** (Admin only or if user system is disabled) Toggles the main background processing tasks of the application. The icon indicates the current state (Play = Stopped, Stop = Running).
    *   **Notifications (<i class="fas fa-bell"></i>):** Opens the Notifications modal. A red dot appears on the bell if there are unread notifications and notifications are enabled.
    *   **Release Notes (<i class="fas fa-file-alt"></i>):** Opens a popup displaying recent changes and commit messages from the project's repository.
*   **Help Button (<i class="fas fa-question-circle"></i>):** (Desktop only) Opens this help modal, displaying content relevant to the current page you are viewing.