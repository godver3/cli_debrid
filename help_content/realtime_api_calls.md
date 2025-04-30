### Real-time API Calls Help

This page displays a live feed of the most recent outgoing API calls made by the application to external services. It provides a detailed, low-level view of interactions as they happen.

**Features:**

*   **Live Feed:** The table updates automatically approximately every second, showing the latest API calls initiated by the application.
*   **Filtering:**
    *   Use the dropdown menu at the top to filter the displayed calls by a specific API **Domain** (e.g., `api.trakt.tv`, `api.real-debrid.com`).
    *   Select `All Domains` to see calls to every external service.
    *   Click the `Filter` button to apply your selection. The filter is applied to subsequent updates.

*   **API Call Table:**
    *   The table lists the most recent API calls, with the newest appearing as the table refreshes.
    *   **Columns:**
        *   `Timestamp`: The date and time when the API call was initiated.
        *   `Domain`: The base domain of the API service being called (e.g., `api.trakt.tv`).
        *   `Endpoint`: The specific path or resource requested on the API domain (e.g., `/users/settings`, `/torrents/addMagnet`).
        *   `Method`: The HTTP method used for the call (e.g., `GET`, `POST`, `PUT`, `DELETE`).
        *   `Status Code`: The HTTP status code returned by the external service in response to the call (e.g., `200` for success, `401` for unauthorized, `429` for rate limited, `500` for server error).
