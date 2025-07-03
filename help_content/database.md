### Database Help

This page provides an interactive view of your collected media items (movies and episodes) stored in the application's database. You can browse, filter, sort, and perform various actions on these items.

**Statistics Panel:**

*   Click the <i class="fas fa-chart-bar"></i> icon (on the far left) to toggle a panel displaying collection statistics:
    *   Total Movies
    *   Total Shows
    *   Total Episodes

**Table View:**

This is the main area displaying your database items.

*   **Content Type Filter:**
    *   Click `Movies` or `Episodes` links above the table to switch the view between these types. Your selection is saved.
*   **Pagination:**
    *   **Alphabetical:** Click letters (`#`, `A`-`Z`) to show items whose titles start with that letter or symbol.
*   **Columns:** The table displays various details about each item. You can customize which columns are visible (see "Column Selector" below).
*   **Item Actions (per row):**
    *   `▶️` (Play): Opens the video player for this item (highly alpha feature).
    *   `X` (Delete): Deletes the item from the database. A confirmation popup appears, asking if you also want to **blacklist** the item to prevent it from being added again automatically.
    *   `↻` (Rescrape): Deletes the item (including associated files and Plex entries) and moves it back to the `Wanted` queue to be searched for again. A confirmation popup appears.
*   **Selection:**
    *   Use the checkboxes in the first column to select individual items for bulk actions.
    *   Click the `Select All` / `Unselect All` button above the table to toggle selection for all *currently visible* items.
    *   Hold `Shift` and click a checkbox to select a range of items between the last clicked checkbox and the current one.
*   **Cell Content:**
    *   Cells with long content might be truncated. Hover over (desktop) or tap (mobile) a truncated cell to see the full content in a tooltip or popup.

**Column Selector:**

*   Click the `Select Columns to Display` button to reveal the column selection interface.
*   **Available Columns:** Shows columns currently hidden from the table.
*   **Selected Columns:** Shows columns currently visible in the table.
*   Use the `>` and `<` buttons to move columns between the lists. Select one or more columns before clicking.
*   Click `Update View` to apply your column selections to the table. Your choices are saved in your browser.

**Filter and Sort:**

This section allows you to refine the items displayed in the table.

*   **Adding/Removing Filters:**
    *   Click `Add Filter` to create a new filter condition row.
    *   Click the `×` button on a filter row to remove it.
*   **Filter Conditions:** Each row defines a filter:
    *   `Select Column`: Choose the database field to filter on (e.g., `title`, `state`, `year`).
    *   `Select Operator`: Choose how to compare (e.g., `equals`, `contains`, `greater than`).
    *   `Value`: Enter the value to compare against. This might be a text box or a dropdown with predefined options (like for the `state` column). For `content_source`, it shows user-friendly list names.
*   **Filter Logic:** Choose how multiple filters are combined:
    *   `AND`: *All* filter conditions must be true for an item to be shown.
    *   `OR`: *Any* of the filter conditions can be true for an item to be shown.
*   **Sorting:**
    *   `Sort Column`: Select a column to sort the results by.
    *   `Sort Order`: Choose `Ascending` (A-Z, 0-9) or `Descending` (Z-A, 9-0).
*   **Applying/Clearing:**
    *   Click `Apply` to update the table with the current filter and sort settings.
    *   Click `Clear Filter & Sort` to remove all filters, reset sorting, and revert to the default view (usually Movies starting with 'A').

**Bulk Actions:**

These actions apply to all currently selected items (using the checkboxes). Buttons are enabled only when items are selected.

*   `Delete Selected`: Deletes all selected items. A confirmation popup appears with an option to blacklist them.
*   `Rescrape Selected`: Performs the "Rescrape" action (delete and move to `Wanted`) on all selected items after confirmation.
*   `Move Selected to Queue...`: A dropdown menu to move selected items directly to a specific processing queue (e.g., `Wanted`, `Blacklisted`, `Sleeping`). Choose a queue and confirm the action.
*   `Change Selected Version...`: A dropdown menu populated with available quality/release versions (from settings). Select a version to apply it to all selected items and confirm.
*   `Mark Selected as Early Release`: Flags the selected items as "Early Release", potentially affecting how they are processed (useful for items released before their official date). Requires confirmation.
