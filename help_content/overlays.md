### Overlay Management Help

This page is the central hub for managing poster overlays across your entire media library. Overlays are dynamically generated images composited onto your movie and TV show posters — displaying media information (resolution, HDR format, audio codec, ratings, etc.) as styled badges rendered directly onto the artwork.

**Prerequisites:**

*   **Overlays Enabled:** The overlay system must be turned on before any generation or sync actions will take place. Open the **Settings** button (gear icon, top right) and toggle "Enable Overlays" to on.
*   **Media Server Connected:** Your media server (Plex or Jellyfin/Emby) must be configured in Settings. Plex requires a Plex data path; Jellyfin/Emby requires a server URL and token.
*   **Library Synced:** Before generating overlays, you must run a **Sync Library** operation (see below) so the system can match your database items to the media server.
*   **At Least One Layout:** You need at least one layout created in the **Layout Builder** before generation is possible.

---

**Page Header Actions (Top Right):**

*   **Settings (gear icon):** Opens the Overlay Settings modal. Contains the most critical configuration options:
    *   **Enable Overlays:** Master on/off switch. When disabled, no automated overlay tasks will run and no manual generation will take place from this page.
    *   **Media Data Path:** The filesystem path to your Plex data directory (where poster bundles are stored), or the equivalent for Jellyfin. This is required for Plex users — without it, poster uploads cannot be persisted.
    *   **Content Check Interval (days):** How frequently the background sync task re-checks all library items for metadata changes (default: 7 days). Lowering this means more frequent automatic re-renders when media info changes.
*   **Regenerate All:** Force re-renders every overlay in your library — movies, TV shows, and seasons — overwriting any previously applied overlays. Use this after making layout changes you want reflected everywhere.
*   **Remove All:** Removes all applied overlays and restores original posters across your entire library. This is a destructive operation — confirm carefully. It does not delete your layouts or badge assets, only the applied overlays.

---

**Statistics Dashboard:**

The stats bar at the top of the page provides a real-time summary of the overlay system's state.

*   **Total Layouts:** How many layouts are saved. Broken down by movies, shows, and seasons. A layout must be assigned before a poster can be generated.
*   **Overlays Applied:** The total count of items that currently have an overlay applied. Broken down by movies, shows, and seasons.
*   **Unprocessed:** Items that exist in the media server and have been synced but have never been run through the overlay system. These are candidates for your next "Generate Library" run.
*   **Cleaned Up:** A running total of orphaned or outdated poster files that have been removed from disk. Use the **reset counter** (↺ icon on the card) to zero this out. Use the **cleanup** (trash icon) to manually purge orphaned backup files, or the **broom icon** to delete all backups and reset all overlays back to pending (a full start-from-scratch).
*   **Failed:** Items where overlay generation encountered an error. Click the card to filter the media grid to failed items only. Use the **reset counter** (↺ icon) to re-queue all failed items for retry.
*   **Backup Size:** The total disk space used by your original poster backups, along with the count of backed-up posters and any orphaned backups (backups for items no longer in your library).

---

**Library Sync Bar:**

Before generating overlays, the system needs to match items in your database to items in your media server so it knows which poster to update. The **Sync Library** button performs this matching operation.

*   Click **Sync Library** to start. The status message updates in real time as matching progresses.
*   You should re-sync after adding new content, or if overlays stop appearing for new items.
*   The sync runs in the background — you can navigate away without interrupting it.
*   Sync is also performed automatically on a schedule when the overlay system is active.

---

**Movies Tab:**

Displays all movies in your library as a poster grid. Each card shows the current poster (with overlay applied, if any) and a status indicator.

*   **Layout Selector:** Choose which saved layout to apply when generating overlays. Only layouts configured for "Movies" or "Both" appear here.
*   **Search:** Filter the grid by title. Useful for finding a specific movie to regenerate or inspect.
*   **Generate Selected:** Generates (or re-generates) overlays only for the items with checkboxes ticked. The selected layout is applied.
*   **Generate Library:** Generates overlays for all movies in your database — not just what is currently shown in the grid. This respects the currently selected layout. Items that already have an up-to-date overlay may be skipped unless forced.
*   **Remove Selected:** Removes the overlay from checked items and restores their original posters.
*   **Select All / Deselect All:** Checkbox to quickly select or deselect every visible item in the grid.
*   **Regenerate button (↺ on hover):** Each poster card has a small circular regenerate button that appears on hover. Clicking it re-renders just that one poster immediately, useful for spot-fixing a single item.
*   **Version badge:** A small label on each card shows how many file versions exist (e.g., "2 versions"). This corresponds to the Versions/Duplicates badge type in layouts.

---

**TV Shows Tab:**

Identical controls to the Movies tab, but operates on TV show posters. Has one additional feature:

*   **Season Posters:** Clicking the poster card for a TV show opens the **Season Posters modal** (see below), allowing you to manage season-level poster overlays for that specific show.

**Season Posters Modal:**

When you click a show's poster in the TV Shows tab, this modal appears.

*   **Season Layout Selector:** Choose a layout to apply to all seasons of this show, or leave it on "Auto (use best available layout)" to let the system pick the most appropriate season layout automatically.
*   **Generate Library:** Generates season overlays for all seasons across all shows in the database (not just this show).
*   **Individual Season Cards:** Each season is shown as a card. You can regenerate or remove the overlay for a specific season individually.

---

**Layouts Tab:**

Lists all saved layouts. A layout defines how badges are arranged on a poster — positions, sizes, badge types, colors, and styling.

*   **Create Layout:** Opens the Layout Builder to create a new layout from scratch. See the **Layout Builder** help page for full details.
*   **Import Layout:** Imports a layout from a previously exported JSON file. Useful for sharing layouts between instances or restoring from backup.
*   **Load Default Layouts:** Imports the bundled default layouts (Movies, Shows, Seasons) — only for any that are not already present. If you have not set up any layouts yet this is the quickest way to get started. If you have previously deleted a default layout, clicking this button restores only the missing ones without affecting anything else. Layouts imported this way are marked with a **Default** chip on their card.
*   **Layout Cards:** Each saved layout is shown as a card with its name, description, media type (Movies, TV Shows, Season, or Both), and a preview of the badge configuration. Cards marked with a teal **Default** chip are the bundled system layouts. From the card you can:
    *   **Edit:** Opens the Layout Builder with this layout loaded for editing.
    *   **Export:** Downloads the layout as a JSON file for backup or sharing.
    *   **Duplicate:** Creates a copy of the layout with a new name.
    *   **Delete:** Permanently removes the layout. Items previously generated with this layout are not affected — their posters remain until removed or regenerated with a different layout.

---

**Badges Tab:**

A summary preview of your badge asset library, showing how many badge types exist, how many variations are populated, and how many are still missing assets.

*   **Open Badge Library:** Links to the dedicated Badge Library page where you can upload, manage, and organize all badge PNG assets. See the **Badge Library** help page for full details.
*   **Badge Types, Total Variations, Assets Uploaded:** Quick stats loaded from the badge system.

---

**Activity Tab:**

A chronological log of every overlay-related action that has occurred in the system — both automated background tasks and manual user-triggered operations.

*   **Type Filter:** Filter the log to a specific action category:
    *   **Overlay Sync** — Background scan that checks all items for metadata updates.
    *   **Cleanup** — Orphaned poster file removal runs.
    *   **Generate / Regenerate** — Manual or automated single-item overlay generation.
    *   **Generate All / Regenerate All** — Full-library generation runs.
    *   **Remove / Remove All** — Overlay removal actions.
    *   **Layout Created / Updated / Deleted** — Layout management events.
    *   **Sync Library** — Library-to-media-server key matching runs.
    *   **Season Generate / Regenerate / Remove** — Season-level poster operations.
    *   **Poster Reset** — Items reset to their original clean poster.
*   **Refresh:** Reloads the activity log to show the latest entries.
*   **Previous / Next pagination:** The log is paginated. Use these buttons to browse older entries.

---

**Poster Review & Reset Modal:**

Accessible from the Overlay Settings or via certain actions, this modal lets you view all posters that currently have an overlay applied and selectively reset individual items back to their clean original poster.

*   **Search:** Filter the grid by title.
*   **Select All / Deselect All / Select Non-Applied:** Bulk selection helpers.
*   **Reset Selected:** Removes the overlay from all selected items and restores the original poster. Overlays will not be automatically re-applied — use Generate Selected if you want to re-apply after reviewing.

---

**How Overlays Work — Background Automation:**

When overlays are enabled, the system runs several background tasks automatically:

1.  **Overlay Sync** runs on a schedule (configurable interval). It scans all library items, checks whether the media metadata has changed, and re-renders posters where needed.
2.  **New Item Detection** — When a new item is added to your library (or a second version/duplicate is collected), the system can immediately generate an overlay for it without waiting for the next scheduled sync.
3.  **Cleanup** — Periodically removes orphaned poster files from disk (leftover backups for items that no longer exist in your library) to recover disk space.

**Automatic vs. Manual Generation:**

*   Automatic generation uses whichever layout is currently saved with the highest priority for that media type.
*   Manual generation from the Movies/TV Shows tab uses whichever layout is currently selected in the layout dropdown at the time you click the button.
*   If you change a layout, existing posters rendered with the old version are not updated automatically — use Regenerate All or Generate Selected to refresh them.

---

**Important Notes:**

*   **Backup Posters:** The first time an overlay is applied to a poster, the original is backed up to disk. All subsequent overlays are composited onto a fresh copy of the original, so your source artwork is never permanently modified.
*   **Jellyfin/Emby:** When using Jellyfin or Emby as your media server (detected automatically when "Symlinked/Local" mode is set and the server type is Jellyfin), poster uploads are done via the Jellyfin API rather than writing to a Plex bundle path.
*   **Plex Data Path:** For Plex users, this must be the path on the host machine where your Plex data directory lives. Incorrect paths will result in overlays being generated but not visible in Plex.
*   **Season layouts vs Show layouts:** A layout can be assigned to "Season Posters" specifically — these only appear in the seasons layout selector. Layouts assigned to "TV Shows" apply to the show-level poster only.

---

**Tips:**

*   Start by running **Sync Library** before any generation — overlays cannot be applied to items that haven't been matched to the media server yet.
*   Create different layouts for movies vs. TV shows vs. season posters to tailor the badge arrangement for each poster aspect ratio.
*   Use **Generate Selected** when testing a new layout on a handful of items before committing to **Generate Library**.
*   Check the **Activity Tab** after running a full generation to confirm it completed successfully and to see how many items were processed.
*   Use the **Failed** stat card to identify any items that errored out, then click it to filter the grid and regenerate just those items.
*   If a layout change should apply to all existing posters, use **Regenerate All** from the page header — it processes movies, shows, and seasons in one pass.
