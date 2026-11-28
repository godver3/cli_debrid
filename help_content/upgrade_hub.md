### Upgrade Hub Help

The Upgrade Hub finds better releases for items you already have, lets you review them, and queues the ones you pick. It also holds every upgrade setting. It is a beta feature, so check a suggestion before you queue it.

**Where candidates come from:**

*   **Your scrapers.** A scan can search your enabled scrapers for items released recently (see **Fresh Release Search**), and **Search Recent Releases** does the same on demand.
*   **Bulk Sources.** These check the whole library on every scan: **Zilean** (its database) and **NZB indexers** (see below). Pick them in the Settings tab.
*   **Only items in the Collected state** are scanned. Items still downloading or in other queues are skipped.

At least one source must be available, or the scan will not start: Zilean or a Newznab indexer set up in Settings → Scrapers, or Fresh Release Search turned on.

---

**Page Header:**

*   **Status text:** Displays the current state — idle, scan in progress, or when the last scan completed.
*   **Scan for Upgrades:** Starts a scan now. A progress bar and label appear during the scan. Results replace the candidates shown before.
*   **Search Recent Releases:** Searches your scrapers for a better release of every collected item released in the last N days. A popup asks for the number of days (it starts at your **Release Age**), then tells you how many items it will search and asks you to confirm. At most 2,000 items are searched per run, the ones checked longest ago first. Run it again for the rest. Zilean and the NZB feed history are skipped. Whatever it finds is added to your current results.
*   **Summary chips:** After a scan completes, a row of chips appears showing totals: upgrades found, packs found, movies scanned, episodes scanned, and any errors encountered. A "Direct DB" chip appears if the scan fell back to direct database access.

---

**Upgrades Tab:**

Displays individual item upgrade candidates — movies and episodes where a better release was found. The tab badge shows the total count of upgrade candidates.

*   **Filter bar:** Text or regular expression search against the title, current filename, candidate filename, type, and version fields.
*   **Type filter:** Narrow the view to **All**, **Movies**, or **Shows** (episodes) using the pill buttons.
*   **Recent Only toggle:** When active, hides candidates whose best-found torrent was indexed more than the configured number of days ago (see Settings → Recent Threshold). Useful for focusing on newly available upgrades.
*   **Min threshold slider:** Drag to set a minimum improvement percentage. Candidates below the threshold are hidden from the table. The label shows the current minimum (e.g., "+15%").
*   **Select All checkbox (header):** Selects or deselects all currently visible rows.
*   **Ignore Selected:** Adds the selected items to the ignore list so they will not appear as upgrade candidates in future scans. The button shows the current selection count and is disabled until at least one item is checked.
*   **Queue Selected:** Sends the selected candidates to the upgrading queue for download. The button shows the current selection count and is disabled until at least one item is checked.
*   **Columns:**
    *   **Type** — Movie or episode indicator.
    *   **Title** — The media title (with season and episode for TV shows).
    *   **Current / Best Candidate** — Your currently collected file on the left; the best upgrade candidate found on the right.
    *   **Version** — The quality version profile matched (e.g., "1080p", "4K").
    *   **Score Δ** — The improvement percentage between your current file's score and the candidate's score. Higher is better.
    *   **Size** — The candidate torrent's file size.
*   **NZB / Debrid badges:** Show whether the current file and the candidate came from Usenet or from debrid.
*   **Cache badges:** **Cached** or **Not Cached** show what your debrid service reports for the candidate (Real-Debrid through Phalanx, TorBox through its own cache check). **Cached Bypass** means there was no recent Zilean result, but your debrid service says the torrent is cached.
*   **Infinite scroll:** The table renders results in batches as you scroll down, so large result sets load progressively without freezing the page.

---

**Season Packs Tab:**

Displays season-level upgrade candidates — complete season packs that would replace multiple individually collected episodes. The tab badge shows the total count of pack candidates.

*   **Filter bar:** Text or regex search against the show title and season.
*   **Recent Only toggle:** Same behaviour as in the Upgrades tab.
*   **Min threshold slider:** Same behaviour as in the Upgrades tab.
*   **Ignore Selected / Queue Selected:** Same behaviour as in the Upgrades tab, but operate on pack candidates.
*   **Columns:**
    *   **Show** — The TV show title.
    *   **Season** — The season number.
    *   **Collected** — How many episodes from this season are already collected.
    *   **Current / Best Pack** — Your current best episode file on the left; the best season pack candidate on the right.
    *   **Version** — The quality version profile matched.
    *   **Score** — The candidate pack's quality score.
    *   **Size** — The candidate torrent's file size.

---

**Settings Tab:**

All upgrade settings live here. Changes take effect after clicking **Save Settings**.

**Upgrading** (what happens right after an item is collected)

*   **Keep Searching After Collecting:** Newly collected items that are recent releases go to the `Upgrading` queue and are searched again every hour during the upgrade window. A better release replaces the file.
*   **Release Age:** Only items released this many days ago or less get the upgrade window. Default is 7 days. A big number means many more hourly searches. This is also the default for **Search Recent Releases**.
*   **Upgrade Window:** How many hours a new item keeps being searched before it settles as Collected.
*   **Score Improvement Needed:** How much higher (in %) a release must score than your current file to replace it. Scans use it too.
*   **Stop at Score:** Items that already score this or higher are not searched again. 0 means no limit.
*   **One Extra Search After Release:** Searches once more this many days after release, when better encodes have usually appeared. 0 turns it off.
*   **Remove the Old Copy:** After an upgrade is collected, removes the original from the media server and the debrid service.

**Automatic Scan**

*   The schedule itself is turned on or off on the Automation page ("Upgrade Hub scan"). While it is on, an hourly **Upgrade Hub NZB feed sync** task also reads the newest releases from each Newznab indexer (usually one or two API calls each) and keeps the ones that match your library for 90 days. Scans check that history.
*   **Score Threshold:** Minimum improvement to show a candidate in both tabs. The sliders in each tab can override it for that session.
*   **Only Show Recent Items:** The Recent Only filter is on by default when the page loads.
*   **Recent Threshold:** How many days count as "recent". Default is 90.
*   **Fresh Release Search:** Off by default. When on, each scan also searches your scrapers for items released within the given number of days (default 30), up to the given number of items per scan (default 20). Each item is a full search with one API call per indexer, so keep the limit low. Items checked longest ago go first.
*   **Scan Limit:** Maximum number of Collected items to scan per run: All, 100, 500, 1000, 2000 or 5000.

**Auto Queue**

*   The schedule is turned on or off on the Automation page ("Upgrade Hub auto-queue"). After each scheduled scan it queues candidates above the Score Threshold, up to **Upgrades Per Run**. Cached candidates go first, and ones reported not cached are skipped unless you accept uncached results.
*   **Hide Episodes Already in Season Packs:** Hides single-episode candidates that a Season Pack candidate already covers.
*   **Upgrades Per Run:** Maximum items queued per run, counted separately for Upgrades and Season Packs. Default is 10.
*   **Exclude NAS Items:** Skips items whose file path starts with a NAS path set in Settings → Library & Files → Library Management → NAS / Network Drive.
*   **Bulk Sources:** Which library-wide sources a scan reads, besides your scrapers: **Both (Zilean + NZB Indexers)**, **Zilean Only**, **NZB Indexers Only** or **None (fresh-release search only)**. Zilean is fast with the direct DB connection in Settings → Scrapers and slow without it.
*   **Excluded Genres:** Items in these genres are skipped during scans, for movies and shows. Search for a genre to add it and remove a chip to delete it.

**Settings Footer Actions**

*   **Save Settings:** Persists all current setting values.
*   **Clear Hub Queue:** Removes all items that were queued via the Upgrade Hub from the pre-queue, allowing a fresh queue to be built. Does not affect items already being actively downloaded.
*   **Run Upgrade Cleanup:** Removes old files and torrents from debrid for items that have already been successfully upgraded and collected in their new version.

---

**Activity Tab:**

A chronological log of every action performed by the Upgrade Hub — both manual user-triggered operations and automated scheduled runs. Entries are kept for 30 days.

*   **Type filter:** Filter the log to a specific action category:
    *   **All actions** — Every recorded event.
    *   **Scan** — Scan runs, including totals for upgrades found, packs found, items scanned, and any errors.
    *   **Queue** — Queue operations, including how many items were queued and which ones.
    *   **Upgrade Processed** — Individual upgrade outcomes after a hub-queued item completes, showing the old and new filenames.
*   **Refresh:** Reloads the activity log to show the latest entries.
*   **Columns:**
    *   **Date / Time** — When the action occurred.
    *   **Action** — The type of action (Scan, Queue, Upgrade Processed).
    *   **By** — Whether the action was triggered manually or by a scheduled task.
    *   **Result** — Success, failed, or partial.
    *   **Details** — A summary of the outcome with key statistics.
*   **Prev / Next pagination:** The log is paginated at 50 entries per page.

---

**How the Upgrade Hub Works:**

1.  **Scan:** Clicking Scan for Upgrades (or the scheduled task running) checks each Collected item against the sources you chose: Zilean, the NZB feed history and, if Fresh Release Search is on, your scrapers. Results are scored with the same version rules as the main scraper. A filter-out term set to Debrid or NZB in a version only applies to releases from that source.
2.  **Candidate Selection:** Items where a higher-scoring torrent is found are presented as upgrade candidates. Season packs covering multiple collected episodes are identified separately and shown in the Season Packs tab.
3.  **Review:** Use the filters, type pills, Recent Only toggle, and threshold slider to focus on the candidates most relevant to you.
4.  **Queue:** Select the items you want and click Queue Selected. Selected items are added to the hub pre-queue and will be processed by the upgrading queue on the next cycle.
5.  **Ignore:** Items you do not want to upgrade can be ignored. Ignored items are excluded from all future scans and will not appear as candidates again.
6.  **Auto Queue:** If Auto Queue is enabled, the above steps happen automatically after each scheduled scan — candidates above the score threshold are queued without manual intervention, up to the Upgrades Per Run limit.

---

**Important Notes:**

*   **Zilean is optional.** If you use it, it must be reachable, or the Zilean part of a scan fails. You can run on scrapers and NZB indexers only.
*   **Scan time scales with library size.** Scanning 10,000+ collected items can take several minutes. Use the Scan Limit setting to cap scan time if needed.
*   **Queuing is not immediate download.** Queued items enter the hub pre-queue and are processed by the upgrading queue on schedule — they do not bypass the normal upgrading queue limits.
*   **Ignore list is permanent.** Items added to the ignore list are excluded from all future scans. Currently there is no UI to manage the ignore list directly — clear it via the database if needed.
*   **Season packs replace episodes.** When a season pack is queued and successfully downloaded, it replaces the individually collected episode files. Ensure you have sufficient debrid slots before queuing large packs.

---

**Tips:**

*   Run a scan first with no filters applied to get a complete picture of available upgrades, then use the threshold slider to focus on the highest-quality improvements.
*   Use the **Recent Only** filter to quickly find upgrades for content that has recently had a new high-quality release (e.g., a 4K remux released after you originally collected a 1080p version).
*   Enable **Hide Episodes Already in Season Packs** before queuing from the Upgrades tab to avoid downloading individual episodes that will shortly be replaced by a season pack.
*   Check the **Activity tab** after an auto-queue run to confirm what was queued and review any failures.
*   Use **Run Upgrade Cleanup** periodically after upgrades have been processed to remove superseded torrents and recover debrid slots.
