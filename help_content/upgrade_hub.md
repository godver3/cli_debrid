### Upgrade Hub Help

The Upgrade Hub is a dedicated tool for identifying and queuing quality upgrades across your entire collected media library. It scans your collected items against a local Zilean instance to find better versions — higher resolution, improved encoding, or full season packs that replace individual episodes — and lets you review candidates before queuing them for download.

**Prerequisites:**

*   **Local Zilean:** A local Zilean instance is required. Configure the URL in **Settings → Scraper Settings → Zilean Scraper** before using this page.
*   **Collected Items:** The scan only operates on items already in a Collected state in your database — items still being downloaded or in other queue states are skipped.

---

**Page Header:**

*   **Status text:** Displays the current state — idle, scan in progress, or when the last scan completed.
*   **Scan for Upgrades:** Triggers a fresh scan immediately. A progress bar and label appear during the scan. Results replace any previously displayed candidates once the scan completes.
*   **Summary chips:** After a scan completes, a row of chips appears showing totals: upgrades found, packs found, movies scanned, episodes scanned, and any errors encountered. A "Direct DB" chip appears if the scan fell back to direct database access.

---

**Upgrades Tab:**

Displays individual item upgrade candidates — movies and episodes where a better quality version was found in Zilean. The tab badge shows the total count of upgrade candidates.

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

Persistent configuration for the Upgrade Hub. Changes take effect after clicking **Save Settings**.

**Automatic Scan**

*   **Automatic Scan toggle:** When enabled, the hub will run a scan automatically every 24 hours. Requires the corresponding task to also be enabled in the Task Manager (Settings → Task Manager → Features tab).
*   **Score Threshold:** Sets a global minimum improvement percentage. Candidates below this threshold are hidden by default in both the Upgrades and Season Packs tabs. This is the saved default; the per-tab sliders in each tab can override it for that session.
*   **Only Show Recent Items:** When enabled, the Recent Only filter is active by default when the page loads. Items older than the Recent Threshold are hidden unless you toggle the filter off manually.
*   **Recent Threshold:** The number of days used to define "recent." Candidates with a best-found torrent indexed within this many days are considered recent. Default is 90 days.
*   **Scan Limit:** Maximum number of Collected items to scan per run. Use this to limit scan time on large libraries or to test results on a subset. Options are All, 100, 500, 1000, 2000, or 5000.

**Auto Queue**

*   **Auto Queue toggle:** When enabled, upgrade candidates above the Score Threshold are automatically queued for download after each scheduled scan completes. Requires Automatic Scan to be active. Respects the Upgrades Per Run limit.
*   **Hide Episodes Already in Season Packs:** When enabled, individual episode upgrade candidates are hidden from the Upgrades tab if the same episode is already covered by a Season Pack candidate. Prevents redundant queuing of episodes that will be replaced by a pack.
*   **Upgrades Per Run:** The maximum number of items queued per category per auto-queue run. Applied independently to Upgrades and Season Packs — for example, a value of 3 allows up to 3 individual upgrades plus up to 3 season packs per run. Default is 10.
*   **Excluded Genres:** Items matching any genre in this list are skipped during scan. Applies to both movies and shows. Use the search field to find and add genres; click a genre chip's remove icon to delete it.

**Settings Footer Actions**

*   **Save Settings:** Persists all current setting values.
*   **Clear Hub Queue:** Removes all items that were queued via the Upgrade Hub from the pre-queue, allowing a fresh queue to be built. Does not affect items already being actively downloaded.
*   **Run Upgrade Cleanup:** Removes old files and torrents from debrid for items that have already been successfully upgraded and collected in their new version.

---

**Activity Tab:**

A chronological log of every action performed by the Upgrade Hub — both manual user-triggered operations and automated scheduled runs. Entries are retained for 30 days.

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

1.  **Scan:** Clicking Scan for Upgrades (or the scheduled task running) queries your local Zilean instance for each Collected item in your database. Zilean is searched by title and returns torrents it has indexed. The results are scored using the same version-matching system used by the main scraper.
2.  **Candidate Selection:** Items where a higher-scoring torrent is found are presented as upgrade candidates. Season packs covering multiple collected episodes are identified separately and shown in the Season Packs tab.
3.  **Review:** Use the filters, type pills, Recent Only toggle, and threshold slider to focus on the candidates most relevant to you.
4.  **Queue:** Select the items you want and click Queue Selected. Selected items are added to the hub pre-queue and will be processed by the upgrading queue on the next cycle.
5.  **Ignore:** Items you do not want to upgrade can be ignored. Ignored items are excluded from all future scans and will not appear as candidates again.
6.  **Auto Queue:** If Auto Queue is enabled, the above steps happen automatically after each scheduled scan — candidates above the score threshold are queued without manual intervention, up to the Upgrades Per Run limit.

---

**Important Notes:**

*   **Zilean is required.** Without a locally running Zilean instance, scans will fail immediately. Zilean must be indexing your region's torrent sources for results to be meaningful.
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
