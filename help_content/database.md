### Database Help

This page lists every movie and episode Clyde knows about. You can browse, filter, sort and change items, one at a time or in bulk.

**Top bar:**

*   **Total Items:** every row in the database.
*   **Unique Items:** rows that match the current filter, with versions of the same movie or episode counted once.
*   **Pages:** how many pages the current filter has.
*   **Per Page:** how many rows load at a time (100, 250, 500 or 1000). Your choice is saved in your browser.
*   **Columns:** opens the column selector.
*   **Reset Cols:** puts column widths back to the defaults.
*   **Filter & Sort:** opens the filter and sort panel.
*   The <i class="fas fa-chart-bar"></i> button opens a small panel with Total Movies, Total Shows and Total Episodes.

**The table:**

*   **All / Movies / Episodes** above the table switch the type. Your choice is saved.
*   **#, A-Z:** the letter links show only titles that start with that letter. `#` is titles that start with a digit or symbol. `All` removes the letter filter.
*   More rows load as you scroll, so there are no page buttons. The button at the bottom right takes you back to the top.
*   Click a column header to sort by it. Click again to flip the order. Drag a column border to resize it.
*   Long cells are cut short. Hover (or tap on mobile) to see the full text.
*   The **Source** column shows whether the item came from debrid or Usenet. It is added to the view automatically.
*   The first column is a checkbox. Hold `Shift` and click a second checkbox to select the whole range between them.

**Row buttons:**

*   **Delete (trash):** removes the item from the database. A popup lets you also **blacklist** it so it is not added again.
*   **Rescrape (circular arrows):** deletes the item, its file on disk and its Plex entry, then puts it in the `Wanted` queue to be searched again. A popup asks you to confirm.
*   **Open in Library (arrow out of a box):** opens the item's Library page in a new tab. Needs a TMDB or IMDb ID.
*   **Move to Wanted (plus):** moves the item to the `Wanted` queue after you confirm.
*   **Blacklist (circle with a slash):** moves the item to `Blacklisted` after you confirm.

**Columns:**

*   Click **Columns** (or **Select Columns to Display**) to open the selector.
*   **Available Columns** are hidden. **Selected Columns** are shown.
*   Select one or more and use `>` or `<` to move them across. `▲` and `▼` change the order of the selected columns.
*   Click **Update View** to apply. The choice is saved in your browser.

**Filter & Sort:**

*   Click **Add Filter** for a new row: pick a column, an operator (such as equals, contains, greater than) and a value. Some columns, like `state` and `content_source`, give you a list of values to pick from.
*   The **Title**, **State**, **Version**, **IMDB ID** and **Type** buttons add a filter row for that column.
*   Click the `×` on a row to remove that filter.
*   **Filter Logic:** `AND` means every filter must match. `OR` means any one is enough.
*   **Sort Column** and **Sort Order** (Ascending or Descending) sort the results.
*   Click **Apply** to use the settings. **Clear all** removes the filters and the sort.
*   The line under the title shows how many results match, how many are unique, and the total pages and items.
*   Your filters, sort and letter are saved in your browser, so the page opens the same way next time.

**Saved Presets:**

*   **Save Preset** saves the current filters, sort, columns, type, letter and rows per page under a name. Presets are kept in your browser only.
*   **Load** in the **Saved Presets** list applies a preset. `✕` deletes it.

**Bulk actions:**

These work on the ticked rows and stay greyed out until you tick something.

*   **Select All:** ticks the rows on screen. A link next to it, **Select all N matching**, picks every row the filter matches, including rows not loaded yet. Ticks stay when you scroll.
*   **Delete Selected:** deletes the items. You can also blacklist them.
*   **Rescrape Selected:** deletes the items, their files and Plex entries, and puts them in `Wanted`.
*   **Move Selected to Queue...:** moves the items to the queue you pick (Adding, Blacklisted, Checking, Final Scrape, Pending Uncached, Scraping, Sleeping, Unreleased, Upgrading, Wanted or Ghostlist).
*   **Change Selected Version...:** sets the version to one of your versions from Settings.
*   **Tags...:** pick one of your tags, then choose **Add** (adds the tag), **Update** (sets the tags to just this one) or **Remove**.
*   **Early Release:** marks the items as early releases, for items out before their official date.
*   **Force Priority:** makes the items jump to the front of their queues.
*   **Resync Selected:** resyncs symlinks and database paths for the items.
*   **Verify Symlinks:** adds the items to the symlink verification queue, which checks the files were picked up by Plex.

Each bulk action asks you to confirm first.
