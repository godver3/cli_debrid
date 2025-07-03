### Connections Help

This page displays the real-time connection status of various internal services and external integrations used by the application. It helps you quickly diagnose if a component or integration is experiencing connectivity issues.

**Page Features:**

*   **Auto-Refresh:** The page automatically refreshes every 30 seconds to provide up-to-date status information.
*   **Status Indicators:** Each connection card features a status indicator dot:
    *   <span style="color: #81C784;">●</span> **Green:** Indicates a successful connection.
    *   <span style="color: #e74c3c;">●</span> **Red:** Indicates a disconnection or an error.

**Connection Sections:**

The connections are grouped into logical sections:

1.  **System Connections:**
    *   Shows the status of core internal components and essential external services.
    *   Cards include:
        *   `cli_battery`: The core background processing service. (Always shown)
        *   `Plex`: Connection to your Plex Media Server. (Only shown if Plex integration is configured)
        *   `Mounted Files`: Status check for configured mounted network drives/paths. (Only shown if configured)
        *   `Phalanx DB`: Connection to the distributed hash checking service. (Only shown if configured and enabled)

2.  **Scraper Connections:**
    *   This section appears if you have configured any scrapers (e.g., Jackett, Torrentio, Nyaa).
    *   Displays a card for each configured scraper, showing its name and connection status.

3.  **Content Source Connections:**
    *   This section appears if you have configured any content sources (e.g., Trakt, Overseerr, MDBList, Plex Watchlists).
    *   Displays a card for each configured source, showing its name and connection status.

**Connection Issues:**

*   If any connections are currently experiencing errors (i.e., their status indicator is red), an additional section titled "Connection Issues" will appear at the bottom.
*   This section lists each failing connection by name and provides details about the specific `Status` or `Error` message reported. This is useful for troubleshooting.
