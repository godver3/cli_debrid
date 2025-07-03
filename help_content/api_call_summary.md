### API Call Summary Help

This page provides a summary of outgoing API calls made by the application to various external services over selected time periods. It helps in monitoring usage, especially for services with rate limits or quotas.

**Features:**

*   **Time Frame Selection:**
    *   Use the dropdown menu at the top to select the granularity of the summary:
        *   `Hourly`: Shows call counts for each hour.
        *   `Daily`: Shows call counts aggregated for each day.
        *   `Monthly`: Shows call counts aggregated for each month.
    *   Click the `Update` button after selecting a time frame to refresh the summary view. The current time frame is shown in the page title (e.g., "API Call Summary (Daily)").

*   **Summary Table:**
    *   The table displays the aggregated API call counts based on the selected time frame.
    *   **Rows:** Each row represents a specific time period (e.g., a specific hour like "14:00", a day like "2023-10-27", or a month like "2023-10").
    *   **Columns:**
        *   `Time Period`: Shows the specific hour, day, or month the row represents.
        *   **Domain Columns:** Each subsequent column represents a specific API domain that the application interacts with (e.g., `api.trakt.tv`, `api.real-debrid.com`, `api.github.com`). The cell value shows the total number of calls made to that domain during that specific time period. A `0` indicates no calls were made to that domain in that period.
        *   `Total`: The last column shows the sum of all API calls made across *all* domains during that specific time period.
