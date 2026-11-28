### Settings Help

This page holds Clyde's settings. The menu on the left groups them, and the search box finds a setting across all tabs.

**General**

*   **UI Settings:** Look and behavior of the interface.
*   **Required Settings:** The basics: media server type and libraries, and your debrid providers with their API keys.

**Media**

*   **Scrapers:** Add and test your scrapers: Zilean, MediaFusion, Comet, AIOStreams, Jackett, Torrentio, Nyaa, Prowlarr and Newznab (Usenet indexers). Comet is a common backup when Torrentio is blocked or down. The public instance `https://comet.feels.legal` needs no setup. Leave Comet's own filters off, because Clyde filters by your versions.
*   **Version Settings:** Your quality versions and their rules.
*   **Scraping:** More scraping options (full installs only).
*   **Content Sources:** Where new items come from (Trakt, MDBList, Plex Watchlist, Overseerr and others).
*   **Tracking accounts:** Trakt and similar accounts.

**Library**

*   **Library & Files:** File management, folders and library cleanup.
*   **Media Server:** Plex, Jellyfin, Emby or Silo.
*   **Subtitles:** Subtitle providers and options.

**System**

*   **Queues & Upgrades:** Queue timing. The upgrade settings now live on the **Upgrade Hub** page.
*   **Performance & Logging:** Resource use and log levels.
*   **Notifications:** Where Clyde sends messages.
*   **AI Assistant:** The AI Butler chat widget, plus a pointer to the **AI Assistant (MCP)** page under Tools, where you connect an outside AI assistant.
*   **Export / Import:** Save your settings to a file or load them back.

**Debrid providers (Required Settings):**

*   The list is ranked. For each release Clyde checks the providers in order and adds it to the first one that has it cached. If one fails (DMCA block, rate limit, outage), it tries the next. Each needs its own API key.
*   Use the arrows to change the order, **Test** to check a key and the bin to remove a row. AllDebrid can only check the cache by adding, so put it last.
*   **Streaming only** appears on each fallback row only when the built-in mount serves debrid. Ticked, Clyde stops adding new releases to that account, while files already on it keep playing and stay visible in the debrid library and repair tools. The first (main) provider cannot be streaming only.

**Important:** Click **Save Settings** after making changes.
