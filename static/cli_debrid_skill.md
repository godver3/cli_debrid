---
name: cli-debrid
description: Query and control the cli_debrid media automation application. Provides live queue status, library search, recently collected items, ability to add movies or shows to the download queue, and actions like starting/stopping the queue, triggering Plex scans, upgrade scans, running tasks, maintenance operations, and a rich live context snapshot for awareness of app state, errors, and upgrade activity.
---

# cli_debrid Integration

You have access to a cli_debrid instance via HTTP. Use the `web_fetch` tool to call these endpoints.

**Base URL:** {{CLI_DEBRID_URL}}
**Token:** {{CLI_DEBRID_TOKEN}}

> Authentication is passed as a query parameter. Append `?token={{CLI_DEBRID_TOKEN}}` to every URL. Do NOT use an Authorization header.
> If no token is shown above, omit the `?token=` parameter.

---

## Staying aware — call context first

Before answering questions about queue state, errors, library stats, or upgrade activity, call the context endpoint to get a live snapshot.

### Get Live Context
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/context?token={{CLI_DEBRID_TOKEN}}
```
Optional params: `&library=true` adds the full collected library IMDB index. `&history=true` adds Trakt/Plex watch history.

Returns: `{ running, uptime, queues, library, statistics, errors: {count, last_warning, recent}, log_tail, upgrade_hub, notifications }`

Use for: any question about current state, errors, stuck items, upgrade activity, recent notifications, or "what's going on?"
Use `&library=true` when asked "is X in my library?" for items not found via search_library.
Use `&history=true` when making content recommendations.

---

## Available Endpoints

### Queue Status
Check what is currently downloading and whether the program is running.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/queue_status?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ running, status, queues: { Wanted, Scraping, Adding, Checking, Collecting, Upgrading, Sleeping, Blacklisted } }`

Use for: "what's downloading?", "is the program running?", "how many items are queued?"

---

### Library Stats
Get overall library counts and items collected in the last 24 hours.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/library_stats?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ collected, wanted, blacklisted, recently_collected_24h: [ {title, type, collected_at} ] }`

Use for: "how many movies do I have?", "what was recently added?", "how big is my library?"

---

### Recently Collected
List the most recently downloaded items.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/recently_collected?limit=20&token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ items: [ {title, year, type, version, collected_at} ], count }`

Use for: "what was just downloaded?", "show me recent additions", "what's new?"

---

### Search Library
Search the collected library by title to check if something is already there.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/search_library?q=TITLE&token={{CLI_DEBRID_TOKEN}}
```
Optional: `&type=movie` or `&type=episode`
Returns: `{ items: [ {title, year, type, state, imdb_id} ], count, query }`

Use for: "is X in my library?", "do I have X?", "is X collected?"
States: Collected, Upgrading, Wanted (in queue but not yet downloaded)

---

### Add to Library
Add a movie or TV show to the download queue. **Always confirm with the user before calling this.**
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/add_to_library?token={{CLI_DEBRID_TOKEN}}
Content-Type: application/json

{ "title": "Show Name", "year": 2021, "media_type": "movie", "imdb_id": "tt1234567" }
```
- `media_type`: `"movie"` or `"tv"`
- `imdb_id`: optional but improves accuracy
- `version`: optional version name (e.g. `"1080p"`). Defaults to the first configured version. Omit to use the default.
- Returns: `{ ok, items_added, title, imdb_id }`

Use for: "add X to my library", "download X", "request X"

---

### Start Queue
Start the program and queue processing.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/queue_start?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "start the program", "resume processing", "start the queue"

---

### Stop Queue
Stop the program and queue processing.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/queue_stop?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "stop the program", "pause processing", "stop the queue"

---

### Plex Scan
Trigger a Plex library scan (runs in background).
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/plex_scan?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "scan Plex", "refresh Plex library", "trigger a Plex scan"

---

### Upgrade Scan
Trigger an upgrade hub scan to find better quality versions.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/upgrade_scan?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "run upgrade scan", "check for upgrades", "scan for better versions"

---

### Available Tasks
List all APScheduler tasks that can be triggered manually.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/available_tasks?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ tasks: [...], count }`

Use for: "what tasks are available?", before calling run_task

---

### Run Task
Trigger a named background task manually.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/run_task?token={{CLI_DEBRID_TOKEN}}
Content-Type: application/json

{ "task_name": "task_heartbeat" }
```
Returns: `{ ok, message, result }`

Use for: "run task X", "manually trigger X". Always call available_tasks first to confirm the task name exists.

---

### Trim Memory
Free unused process memory.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/trim_memory?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "free memory", "trim memory", "memory is high"

---

### Cleanup Failed Upgrades
Clear the failed upgrades history.
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/cleanup_failed_upgrades?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "clean up failed upgrades", "clear failed upgrades"

---

### Remove Duplicates
Remove duplicate items from the database (runs in background).
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/remove_duplicates?token={{CLI_DEBRID_TOKEN}}
```
Returns: `{ ok, message }`

Use for: "remove duplicates", "clean up duplicate entries"

---

### Send Notification
Send a message to the user's configured external notification channels (Discord, Telegram, etc.).
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/send_notification?token={{CLI_DEBRID_TOKEN}}
Content-Type: application/json

{ "title": "AI Butler", "message": "Your message here" }
```
Returns: `{ ok, message }`

Use for: "notify me", "send me a message about X", "ping me when done"

---

## Rules
- Always append `?token={{CLI_DEBRID_TOKEN}}` to every request URL (if token is set). Do NOT use an Authorization header.
- Call `context` first when asked about errors, stuck items, upgrade activity, or current state — it gives you a full live snapshot.
- Search the library first before adding something — tell the user if it's already collected.
- For "add to library" requests, confirm the title and type with the user before POSTing.
- For destructive actions (stop queue, remove duplicates), confirm with the user first.
- For queue_start/stop, inform the user of the impact before proceeding.
- For run_task, always call available_tasks first to verify the task name exists.
- If a request fails with 401, the token is wrong or missing in cli_debrid settings.
- If a request fails with 503, cli_debrid AI Assistant is disabled in settings.

## cli_debrid feature reference
- **Upgrade Hub**: Scans collected items to find better quality versions. Items move to Upgrading state while waiting.
- **Queues**: Wanted → Scraping → Adding → Checking → Collecting → Collected (or Blacklisted/Sleeping)
- **Blacklisted**: Items that failed after all retries. High rates indicate a scraper source problem.
- **Debrid Provider**: Real-Debrid or AllDebrid handles torrent caching.
- **File Management**: Plex (direct integration) or Symlinked/Local (symlinks from debrid mount).
- **Content Sources**: Trakt lists, Overseerr, MDB lists — these feed the Wanted queue.
