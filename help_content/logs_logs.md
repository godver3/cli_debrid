### Logs Help

This page provides a real-time view of the application's logs, allowing you to monitor internal operations, troubleshoot issues, and share diagnostic information.

**Log Display:**

*   **Real-time Updates:** New log entries are streamed automatically using Server-Sent Events (SSE). You don't need to refresh the page.
*   **Syntax Highlighting:** Log entries are color-coded for readability:
    *   `Timestamp:` Gray
    *   `Level:` Colored based on severity (Debug: Blue, Info: Green, Warning: Orange, Error: Red, Critical: Purple).
    *   `Filename:` Orange (Bold)
    *   `Function:` Turquoise
    *   `Line Number:` Gray
    *   `Message:` Colored based on the log level (e.g., error messages are red).
    *   `Durations:` (e.g., `123.45ms`) are highlighted in Yellow (Bold).
*   **Scrolling & Pausing:**
    *   The log view automatically scrolls to the bottom as new logs arrive.
    *   If you scroll up manually, logging will pause (`Logging paused...` indicator appears).
    *   To resume live logging, scroll back to the very bottom or click the `⭳ Bottom` button that appears when you're not at the bottom.
*   **Maximum Logs:** To maintain browser performance, only the most recent logs (up to 1500 entries by default) are displayed. Older entries are automatically removed from the view.

**Controls:**

Located at the top of the log container:

*   **Log Level Filter:**
    *   Use the dropdown menu (default: `All Levels`) to show only logs of a specific severity (Debug, Info, Warning, Error, Critical) or higher.
    *   Your selected level is saved in your browser and remembered across sessions.
*   **"Go to..." Search:**
    *   Use this input box to quickly find specific text within the currently displayed logs.
    *   Matching text will be highlighted.
    *   Navigate between matches using:
        *   `Enter`: Next match
        *   `Shift+Enter`: Previous match
        *   `▲` / `▼` buttons next to the search box.
    *   `⭱ First` / `⭳ Last` buttons jump to the first or last match.
    *   The `X/Y` count shows your current position among the total matches found.
    *   Searching automatically pauses the live log feed. Clear the search box to resume.
*   **"Filter logs..." Input:**
    *   Type text here to filter the *incoming* log stream. Only logs containing the entered text will be displayed.
    *   This filter applies *before* logs are added to the view. Clear the input to see all logs again (respecting the level filter).
*   **`Share Logs` Button:**
    *   Click this button to collect, compress, and upload the application's log files to a temporary sharing service (transfer.sh).
    *   The button will show the progress (Collecting, Compressing, Uploading...).
    *   Upon success:
        *   A command like `wget -qO - <URL> | gzip -dc | lnav` will be automatically copied to your clipboard (if permissions allow). This command downloads, decompresses, and views the logs using `lnav`.
        *   A link (`View Raw Log`) to the raw, compressed log file will appear.
        *   A copy icon allows you to copy the raw URL manually.
    *   This is useful for sharing detailed logs with developers for troubleshooting.
