### AI Assistant (MCP) Help

This page lets Claude (Desktop or Code) or another MCP-capable AI assistant connect to Clyde, so it can explain your settings, read your logs and help troubleshoot. You find it on the **Tools** page as **AI Assistant (MCP)**. Only an admin can open it.

**Read-only:** The assistant can look but never change anything. API keys, tokens and passwords are masked in everything it sees.

**Turning it on:**

*   Tick **Turn on the MCP endpoint**. The first time, Clyde makes a token for you. Untick it to turn the endpoint off again. While it is off, assistants get an error.
*   The URL and token cards appear once it is on.

**URL and token:**

*   **URL:** your Clyde address followed by `/mcp`. It must be reachable from the computer where the assistant runs. Behind a reverse proxy, use your public Clyde address with `/mcp`.
*   **Token:** hidden until you click **Show** (click **Hide** to cover it again). The assistant must send it as `Authorization: Bearer <token>`. It works even if Clyde has no user login set up.
*   **Make a new token** replaces the token after you confirm. Every assistant using the old one stops working until you update it. Do this if the token leaks.
*   Anyone with the token can read your settings (secrets masked), logs and library. Keep it private.

**Connecting:**

*   **Claude Code:** copy the command shown under **Claude Code** and run it in a terminal. It has the form `claude mcp add --transport http clyde <url> --header "Authorization: Bearer <token>"`. The command shows `<token>` until you click **Show**.
*   **Claude Desktop / other clients:** copy the JSON shown under **Claude Desktop / other clients** into the client's MCP config (an `mcpServers` entry named `clyde` with the URL and an `Authorization` header).

**What the assistant can see (tools):**

*   `get_overview`: version, whether Clyde is running, library mode, providers (which debrid accounts get new releases and which are streaming only), NFO mode, connection health and item counts.
*   `list_settings`: without a section, the settings sections. With a section, every setting in it with its value (secrets masked), default, choices and description.
*   `explain_setting`: one setting, with the same details.
*   `search_logs`: searches recent log lines. Choose Clyde's log or the built-in mount's log, a level and a limit.
*   `recent_errors`: the most recent warnings and errors.
*   `queue_status`: whether Clyde is running and how many items are in each state (Wanted, Scraping, Checking and so on).
*   `mount_status`: the built-in mount: engine state, problems, servers, cache and open files.
*   `connection_health`: which services (debrid, Plex, scrapers, content sources) are reachable.
*   `external_scan_status`: the "Scan Mount For External Adds" task: files added outside Clyde, and whether they imported.
*   `lookup_item`: finds movies or episodes by title or IMDb id and shows their state, version, release, file and, if asked, related log lines.
*   `get_help`: these built-in help pages.
*   `prepare_issue_report`: gathers what a bug report needs (version and setup, the masked settings snapshot, recent errors). Nothing is sent anywhere.
