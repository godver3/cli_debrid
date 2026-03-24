---
name: cli-debrid
description: Query and control the cli_debrid media automation application. Provides live queue status, library search, recently collected items, and the ability to add movies or shows to the download queue.
---

# cli_debrid Integration

You have access to a cli_debrid instance via HTTP. Use the `web_fetch` tool to call these endpoints.

**Base URL:** {{CLI_DEBRID_URL}}
**Auth header:** `Authorization: Bearer {{CLI_DEBRID_TOKEN}}`

> If no token is shown above, omit the Authorization header.

---

## Available Endpoints

### Queue Status
Check what is currently downloading and whether the program is running.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/queue_status
```
Returns: `{ running, status, queues: { Wanted, Scraping, Adding, Checking, Collecting, Upgrading, Sleeping, Blacklisted } }`

Use for: "what's downloading?", "is the program running?", "how many items are queued?"

---

### Library Stats
Get overall library counts and items collected in the last 24 hours.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/library_stats
```
Returns: `{ collected, wanted, blacklisted, recently_collected_24h: [ {title, type, collected_at} ] }`

Use for: "how many movies do I have?", "what was recently added?", "how big is my library?"

---

### Recently Collected
List the most recently downloaded items.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/recently_collected?limit=20
```
Returns: `{ items: [ {title, year, type, version, collected_at} ], count }`

Use for: "what was just downloaded?", "show me recent additions", "what's new?"

---

### Search Library
Search the collected library by title to check if something is already there.
```
GET {{CLI_DEBRID_URL}}/api/ai/tools/search_library?q=TITLE
```
Optional: `&type=movie` or `&type=episode`
Returns: `{ items: [ {title, year, type, state, imdb_id} ], count, query }`

Use for: "is X in my library?", "do I have X?", "is X collected?"
States: Collected, Upgrading, Wanted (in queue but not yet downloaded)

---

### Add to Library
Add a movie or TV show to the download queue. **Always confirm with the user before calling this.**
```
POST {{CLI_DEBRID_URL}}/api/ai/tools/add_to_library
Content-Type: application/json

{ "title": "Show Name", "year": 2021, "media_type": "movie", "imdb_id": "tt1234567" }
```
- `media_type`: `"movie"` or `"tv"`
- `imdb_id`: optional but improves accuracy
- Returns: `{ ok, items_added, title, imdb_id }`

Use for: "add X to my library", "download X", "request X"

---

## Rules
- Always include the `Authorization: Bearer {{CLI_DEBRID_TOKEN}}` header on every request (if token is set).
- Search the library first before adding something — tell the user if it's already collected.
- For "add to library" requests, confirm the title and type with the user before POSTing.
- If a request fails with 401, the token is wrong or missing in cli_debrid settings.
- If a request fails with 503, cli_debrid AI Assistant is disabled in settings.
