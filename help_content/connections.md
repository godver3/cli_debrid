### Connections Help

This page checks whether Clyde can reach the services it depends on. Use it to find out which one is down.

**Checking:**

*   The checks run when you open the page. Cards show **Checking** until their result arrives, then **Online** or **Offline**.
*   Click **Refresh All** to run every check again. The page does not refresh itself. The "checked ... ago" text next to the count only shows how old the last check is.
*   If a check fails or times out, the card shows **Offline**.

**Top of the page:**

*   **Total**, **Online** and **Offline** counts.
*   **All / Online / Offline** buttons show only the cards with that status.
*   **Filter connections...** searches by name. While you use a button or the search box, the cards show in one list without section headings.

**Sections:**

Each section title has a count such as `3/4`, meaning 3 of 4 are online. The count turns red when anything in the section is offline.

1.  **System Connections:** `cli_battery` is always shown. The others appear when they apply to your setup: Plex, Jellyfin/Emby, Silo, Mounted Files, Phalanx DB, TVDB, TMDB, Scrob, Trakt, and cli_mount (or the Usenet provider name).
2.  **Torrent Scrapers:** one card per scraper you have enabled in Settings → Scrapers (Torrentio, Comet, Jackett, Prowlarr, Zilean, Nyaa, MediaFusion, AIOStreams and so on).
3.  **Usenet Indexers:** one card per Newznab indexer. They have their own section because they show extra lines:
    *   **Expires** or **renews** date of your subscription, when the indexer reports it. It turns orange within 14 days of the end and red once it has passed.
    *   **Usage**, for example `API 123/5000 · Grabs 4/100 today`. It counts the searches and NZB grabs Clyde has made against that indexer, with the limit if you have set one. Some indexers count over the last 24 hours instead of per day.
4.  **Content Source Connections:** one card per content source you have set up (Trakt, MDBList, Plex Watchlist, Overseerr and so on).

A scraper card is named after the instance (for example `NZBGeek_1`) with its type underneath (for example `Newznab`).

**Connection Issues:**

*   If anything is offline, a **Connection Issues** box appears at the bottom with the name of each one. Open the matching settings page and check the address, key or login, or check the service itself.
