# NzbDAV usenet provider

This document covers the nzbdav usenet-backend integration in cli-debrid:
what it does, how to configure it, and an optional category-routing extension
that makes its grabs visible in Plex.

[nzbdav](https://github.com/nzbdav-dev/nzbdav) is a SABnzbd-compatible WebDAV
server. This integration adds it alongside the existing Decypharr backend.

## Why

Decypharr's SAB-compatible API uses provider-specific endpoints
(`/api/add`, `/api/browse/nzbs`, `/api/torrents`). nzbdav implements the
standard SABnzbd query-string API (`/api?mode=…`) instead, so the existing
`DecypharrClient` cannot talk to it.

This patch adds a parallel `NzbdavClient` with identical public interface and
a factory function that selects the active provider via a config key. No
existing call sites are modified.

## Files

| File | Status | Purpose |
|---|---|---|
| `app/usenet/__init__.py` | **NEW** | Provider factory: `get_usenet_client()` selects backend via config key `Usenet Provider.provider` (default: `decypharr` for backwards compat) |
| `app/usenet/nzbdav_client.py` | **NEW** | nzbdav implementation mirroring the `DecypharrClient` interface 1:1 |
| `app/usenet/decypharr_client.py` | unchanged | original — kept untouched |

## Backwards compatibility

- Existing installs default to `provider: decypharr` (or absent config →
  defaults to decypharr). No behaviour change.
- All 11 existing call sites that `import get_decypharr_client` keep working.
- New installs / opt-in users set `Usenet Provider.provider: nzbdav` in config.
- A follow-up cleanup PR could rewrite call sites to use
  `from usenet import get_usenet_client` for provider-agnostic code paths.

## Config

```json
"Usenet Provider": {
  "enabled": true,
  "provider": "nzbdav",                                  // NEW key — "decypharr" | "nzbdav"
  "url": "http://192.168.1.x:3000",                      // nzbdav default port
  "api_token": "<api.key from nzbdav ConfigItems>",      // nzbdav uses apikey query param
  "download_folder": "cli_debrid",                       // mapped to nzbdav category
  "mounted_file_location": "/mnt/remote/nzbdav/__all__", // optional; used for filesystem browse
  "data_path": "",
  "enable_nzb_naming": false,
  "retention_days": 1500
}
```

The category referenced via `download_folder` (or per-grab `category`) must
exist in nzbdav's `api.categories` ConfigItem. Default-shipped categories are
`audio,software,tv,movies`; add `cli_debrid` (or whatever name is used here)
to that list. UI panel: nzbdav Settings → Categories.

## Interface differences (nzbdav has no equivalent)

| DecypharrClient feature | NzbdavClient behaviour |
|---|---|
| `arr` and `downloadFolder` form fields | mapped to nzbdav `cat` query param |
| Server-side URL fetch fallback to pre-fetch | nzbdav supports `mode=addurl`; falls back to pre-fetch + `mode=addfile` if the server-side fetch fails |
| `/api/browse/nzbs/<name>` folder lookup | nzbdav has no browse API → falls back to filesystem walk under `mounted_file_location` |
| `/api/repair/health/<name>/check` trigger | nzbdav's `HealthCheckService` runs internally → `trigger_health_check` / `poll_health_result` are no-ops returning success/None |

## Testing this patch

```bash
# inside the cli-debrid container
docker cp app/usenet/nzbdav_client.py cli_debrid:/app/usenet/
docker cp app/usenet/__init__.py cli_debrid:/app/usenet/
docker restart cli_debrid

# in cli-debrid's Settings UI:
#   Usenet Provider → url=http://192.168.1.x:3000
#                     api_token=<nzbdav api.key>
#                     provider=nzbdav         (new key)
#                     download_folder=cli_debrid  (or another existing category)
#                     mounted_file_location=/mnt/remote/nzbdav/__all__
#
# Connectivity test in the Connections page should now report nzbdav OK.
```

---

# Category-routing extension (heuristic SAB-cat-routing)

The original PR sends every grab to a single static SAB category (`download_folder`, default `cli_debrid`). In a typical Plex setup, that folder is not a library location, so cli_debrid Usenet grabs stay invisible and cli_debrid loops items in `Wanted` / `Checking`.

This extension adds a title-based heuristic in `nzbdav_client.py` that picks a sensible per-type category before submission. The category names mirror zurg's filter-folder naming so the same Plex library locations can feed from multiple providers.

**Why decypharr doesn't have this problem.** Decypharr post-processes downloads with `custom_folder` filters that move files into `/movies`, `/shows`, `/music`. The default `cli_debrid/` folder under decypharr stays empty. nzbdav is a pure SAB-compatible WebDAV server with no post-processing — whatever the client sends, that's where the item stays.

## Title → category mapping

| Title pattern | nzbdav category |
|---|---|
| Show + 1080p + (x264/h264/AVC) | `shows_1080p_264` |
| Show (everything else) | `shows` |
| Movie (year present) + 1080p + (x264/h264/AVC) | `movies_1080p_264` |
| Movie (everything else) | `movies` |
| Music markers (FLAC, MP3, hi-res, …) | `music` |
| Nothing detected | fallback to `default_category` (default `__unplayable__`) |

Centralized in one file — covers all 11 `add_nzb` call sites in cli_debrid (`torrent_processor x3`, `repair_engine x2`, `debrid_manager_routes x2`, `magnet_routes x1`, `scraper_routes x3`), including manual UI submits. No upstream code changes required.

## Section A — For humans

Required setup. Optional enhancements (mergerfs-union, Smart Collections, separate quality libraries) live in their own section further down.

### A.1 nzbdav: add the required categories

Out of the box nzbdav ships with `audio,software,tv,movies`. Add the categories this patch routes into:

```bash
DB=<nzbdav-config-dir>/db.sqlite
sqlite3 "$DB" "SELECT ConfigValue FROM ConfigItems WHERE ConfigName='api.categories'"   # current cats
sqlite3 "$DB" "UPDATE ConfigItems SET ConfigValue=ConfigValue || ',movies,shows,music,__unplayable__,movies_1080p_264,shows_1080p_264' WHERE ConfigName='api.categories'"
docker restart nzbdav
curl -s "http://<nzbdav-host>:3000/api?mode=get_config&apikey=<key>" | jq '.config.categories[].name'   # verify
```

### A.2 cli_debrid: bind mounts and config

In `cli_debrid/docker-compose.yml`, mount the patch files into the container:
```yaml
volumes:
  - /path/to/cli_debrid_nzbdav_patch/app/usenet/nzbdav_client.py:/app/usenet/nzbdav_client.py:ro
  - /path/to/cli_debrid_nzbdav_patch/app/usenet/__init__.py:/app/usenet/__init__.py:ro
  # ... existing bind mounts (debrid provider, nzbdav, etc.)
```

In cli_debrid `config.json`:
```json
"Usenet Provider": {
  "provider": "nzbdav",
  "url": "http://<nzbdav-host>:3000",
  "api_token": "<api.key>",
  "download_folder": "__unplayable__",
  "mounted_file_location": "/mnt/nzbdav"
}
```

`download_folder` is the fallback category for unmatched titles. `mounted_file_location` is what the nzbdav client uses for its own browse helpers (the patch strips a trailing `/__all__` for compatibility with rclone-style paths).

### A.3 Plex: add the new category paths to your existing libraries

Plex Settings → Libraries → Edit each library → Add Folder:

| Plex library | Add these paths |
|---|---|
| Movies | `<your-nzbdav-mount>/content/movies`, `<your-nzbdav-mount>/content/movies_1080p_264` |
| Shows | `<your-nzbdav-mount>/content/shows`, `<your-nzbdav-mount>/content/shows_1080p_264` |
| Music | `<your-nzbdav-mount>/content/music` |

Two paths per video library because the heuristic splits 1080p+H.264 into its own category. Both belong in the main library so all grabs are visible.

**Disable auto-scan on FUSE-backed paths.** inotify does NOT propagate through rclone/WebDAV mounts. Plex Settings → Library → "Update my library automatically" → OFF for the affected sections. cli_debrid triggers Plex scans itself on file discovery.

### A.4 Restart cli_debrid

```bash
docker compose -f cli_debrid/docker-compose.yml up -d --force-recreate cli_debrid
```

### A.5 Verify

```bash
# Heuristic unit-test, standalone (no Flask context required):
python3 -c "
import re
src = open('app/usenet/nzbdav_client.py').read()
start = src.index('# Title-based category detection.')
end = src.index('class NzbdavClient')
ns = {'re': re}
exec(src[start:end], ns)
fn = ns['_detect_category_from_title']
print(fn('Some.Show.S01E01.1080p.WEB-DL.x264-GROUP'))  # -> shows_1080p_264
print(fn('Some.Movie.2024.2160p.UHD.x265-GROUP'))       # -> movies
print(fn('Random.FLAC.Album'))                          # -> music
print(fn('weird_no_markers'))                           # -> ''
"
```

After restart, watch the host-side mount: the next grab should appear under `<your-nzbdav-mount>/content/<expected>/`.

### Pre-existing orphans (for upgraders)

If you already have grabs sitting under `/content/cli_debrid/` from before the patch:

```bash
NZBKEY=<api.key>
NZBHOST=http://<nzbdav-host>:3000
curl -s "$NZBHOST/api?mode=history&apikey=$NZBKEY&category=cli_debrid&limit=500" \
  | python3 -c "import json,sys; [print(s['nzo_id']) for s in json.load(sys.stdin)['history']['slots']]" \
  > /tmp/orphan_ids.txt
while IFS= read -r ID; do
  curl -s "$NZBHOST/api?mode=history&name=delete&value=$ID&apikey=$NZBKEY" > /dev/null
done < /tmp/orphan_ids.txt
```

**Known nzbdav limitation**: SAB-history-delete does NOT clean DavItems folders (orphan bug). The empty folder shells stay in the WebDAV tree. They can confuse cli_debrid's `os.path.exists` file-check if the mergerfs-union (optional enhancement) includes `/content/cli_debrid` as a branch — leave it out of the union, or skip the union altogether.

If cli_debrid flagged items as `Collected` while orphan folders were still visible, reset them to `Wanted`:
```sql
UPDATE media_items SET state='Wanted', filled_by_title=NULL, filled_by_file=NULL
WHERE state='Collected' AND collected_at > '<timestamp-before-patch>'
  AND filled_by_title IN ('<orphan-job-name>', ...);
```

---

## Optional enhancements

Each subsection here is independent — skip any you don't need.

### Delete usenet items natively from Plex

By default nzbdav serves its WebDAV read-only (`webdav.enforce-readonly = true`), so
Plex's "Delete" silently fails: the file DELETE that rclone forwards is answered with
`403`. To delete straight from the Plex UI, set `webdav.enforce-readonly = false`.

- **In-app (recommended):** Settings → Usenet Provider → NzbDAV setup helper →
  section 2 → **Enable delete-from-Plex**. Backed by `POST /program_operation/api/nzbdav/set_delete_mode`
  (writes via nzbdav's `update-config`, applied live, no restart). The same panel's
  **Test connection** now reports the current delete state.
- **In nzbdav:** Settings → WebDAV → uncheck **Enforce Read-Only**.

A Plex delete then removes the file **and** its underlying DavItem — unlike the
SAB-history-delete path (see *Interface differences*), which leaves orphan DavItem
shells. Trade-off: with read-only off the whole nzbdav `/content` tree is deletable
by any WebDAV/rclone client, so mind Plex's trash / "empty trash" / scan settings.
Requires `DISABLE_FRONTEND_AUTH=true` for the in-app button (same as the category check).

### Mergerfs-union for robust direct file detection

cli_debrid's `Plex.mounted_file_location` is a single path. To let cli_debrid verify both debrid- and Usenet-side grabs through one path, you need a flat aggregate view. mergerfs is the standard tool. The companion compose at `../debrid-usenet-union/docker-compose.yml` builds one.

Branches included:
- `<your-debrid-mount>/__all__` (debrid content as a flat aggregate, if your debrid provider exposes one — zurg does)
- `<your-nzbdav-mount>/content/{movies,shows,music,movies_1080p_264,shows_1080p_264,__unplayable__}` (the categories this patch routes into)

Output: `/mnt/debrid-usenet-union/`. cli_debrid then uses this as `Plex.mounted_file_location`.

```bash
cd debrid-usenet-union/
docker compose build && docker compose up -d
ls /mnt/debrid-usenet-union/ | wc -l   # should be >300 once populated
```

In cli_debrid `config.json`:
```json
"Plex": { "mounted_file_location": "/mnt/debrid-usenet-union" }
```

Plus the corresponding bind in cli_debrid's docker-compose:
```yaml
volumes:
  - /path/to/debrid-usenet-union/mnt:/mnt/debrid-usenet-union:rslave
```

**Skip this if you don't need direct file detection.** The patch alone routes grabs to Plex-visible paths; Plex's `recentlyAdded` will mark them Collected once a Plex scan picks them up.

### Smart Collections via Plex API (quality-filter views)

If you want a 1080p-H.264 view of your Movies/Shows libraries without creating separate Plex libraries, create Smart Collections. The filter URI defines what items appear — Plex auto-updates the collection as the library grows.

```bash
PLEX_TOKEN=<your-plex-token>
PLEX_HOST=http://<plex-host>:32400
MACHINE_ID=$(grep -oP 'ProcessedMachineIdentifier="\K[^"]+' /path/to/plex/config/Plex\ Media\ Server/Preferences.xml)

create_smart_collection() {
  local SECTION_ID=$1 TYPE_NUM=$2 TITLE=$3
  local TITLE_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$TITLE")
  local URI_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('server://${MACHINE_ID}/com.plexapp.plugins.library/library/sections/${SECTION_ID}/all?type=${TYPE_NUM}&resolution=1080&videoCodec=h264', safe=''))")
  curl -s -X POST "${PLEX_HOST}/library/collections?type=${TYPE_NUM}&title=${TITLE_ENC}&smart=1&sectionId=${SECTION_ID}&uri=${URI_ENC}&X-Plex-Token=${PLEX_TOKEN}"
}

# substitute your section IDs — find them via curl "${PLEX_HOST}/library/sections?X-Plex-Token=${PLEX_TOKEN}"
# type=1 movie, type=2 show
create_smart_collection <movies-section-id> 1 "1080p H.264 Movies"
create_smart_collection <shows-section-id>  2 "1080p H.264 Shows"
```

To delete a collection later: `curl -X DELETE "${PLEX_HOST}/library/collections/<ratingKey>?X-Plex-Token=${PLEX_TOKEN}"`.

### Separate `_1080p` Plex libraries (legacy overlap pattern)

If you prefer a separate Plex library for the 1080p H.264 subset (rather than a Smart Collection inside the main library), use the patch's `_1080p_264` category paths as the exclusive location for that library:

| Plex library | Locations |
|---|---|
| Movies_1080p | `<your-nzbdav-mount>/content/movies_1080p_264` |
| Shows_1080p | `<your-nzbdav-mount>/content/shows_1080p_264` |

The same paths are already in your Movies / Shows libraries (per A.3) — that's intentional. Plex deduplicates per media GUID, so items appear in both the general and the quality library without filesystem-side duplication.

Trade-off vs. Smart Collections: items show up duplicated in Recently Added / On Deck / global search. Watched-state syncs across both via GUID (often a feature, sometimes a surprise).

### `__unplayable__` fallback visibility

The heuristic catches the vast majority of scene-named titles. Edge-case fallbacks land in `<your-nzbdav-mount>/content/__unplayable__` and stay invisible in Plex unless you add that path to all three type-libraries. Plex's library-type filter (movie/show/artist) handles the cross-content mix automatically. Skip for simplicity; add only if you want full visibility for unmatched grabs.

---

## Section B — For LLMs / AI agents

### Patch architecture

**Single-file patch**: `app/usenet/nzbdav_client.py`. Three edits total.

**Code anchor 1** (module level, after `_VIDEO_EXTS`):
```python
_SHOW_PATTERN   = re.compile(r'\bS\d{1,2}E\d{1,3}\b|\bS\d{1,2}\b|\bSeason[.\s_]\d+\b', re.IGNORECASE)
_MUSIC_PATTERN  = re.compile(r'\b(FLAC|MP3|320[\s_-]?kbps|hi-?res|discography)\b', re.IGNORECASE)
_MOVIE_YEAR     = re.compile(r'\b(19[5-9]\d|20[0-3]\d)\b')
_QUALITY_1080P  = re.compile(r'\b1080p\b', re.IGNORECASE)
_CODEC_H264     = re.compile(r'\b(?:x264|h\.?264|avc)\b', re.IGNORECASE)

def _detect_category_from_title(title: str) -> str:
    if not title: return ''
    is_1080p_h264 = bool(_QUALITY_1080P.search(title) and _CODEC_H264.search(title))
    if _SHOW_PATTERN.search(title):
        return 'shows_1080p_264' if is_1080p_h264 else 'shows'
    if _MUSIC_PATTERN.search(title):
        return 'music'
    if _MOVIE_YEAR.search(title):
        return 'movies_1080p_264' if is_1080p_h264 else 'movies'
    return ''
```

**Code anchor 2** (in `NzbdavClient.__init__`):
```python
# OLD:  self.default_category = cfg.get('download_folder', '') or 'cli_debrid'
self.default_category = cfg.get('download_folder', '') or '__unplayable__'
```

**Code anchor 3** (in `add_nzb_content` and `add_nzb`, on the `cat = ...` line):
```python
# OLD:  cat = category or self.default_category
cat = category or _detect_category_from_title(title) or self.default_category
```

### How to apply this patch autonomously (LLM checklist)

1. **Verify nzbdav is reachable and inspect categories**:
   ```
   curl <nzbdav>:3000/api?mode=get_config&apikey=<key>
   ```
   Parse `.config.categories[].name`; flag any missing of `movies, shows, music, __unplayable__, movies_1080p_264, shows_1080p_264`.

2. **Add missing nzbdav cats** via direct DB UPDATE:
   ```
   sqlite3 <nzbdav-config-dir>/db.sqlite "UPDATE ConfigItems SET ConfigValue=<extended-list> WHERE ConfigName='api.categories'"
   docker restart nzbdav
   ```
   Wait ~6s for ready (poll `mode=version`).

3. **Apply the nzbdav_client.py patch** via Edit tool to the overlay-dir file. Re-verify syntax: `python3 -c "import ast; ast.parse(open('...').read())"`.

4. **Optional mergerfs-union setup** (skip if direct file detection is not required):
   - Create `debrid-usenet-union/docker-compose.yml` with the mergerfs container.
   - Branches: `zurg/__all__` plus all nzbdav content cats EXCEPT `cli_debrid` (orphan bug).
   - `docker compose up -d` in that directory.

5. **Edit cli_debrid `docker-compose.yml`**: ensure overlay bind mounts are present; if A.2 is enabled, add the union bind for `Plex.mounted_file_location`.

6. **Edit cli_debrid `config.json`**:
   - `Plex.mounted_file_location = "/mnt/debrid-usenet-union"` (or `/mnt/zurg/__all__` without the union)
   - `Usenet Provider.download_folder = "__unplayable__"`
   - `Usenet Provider.mounted_file_location = "/mnt/nzbdav"`

7. **Restart cli_debrid**: `docker compose -f cli_debrid/docker-compose.yml up -d --force-recreate cli_debrid`. Wait ~10s.

8. **Verify**: trigger a test grab (via UI or API); within 30s check that `/mnt/nzbdav/content/<expected-cat>/<release>/` exists.

9. **Pre-existing orphan cleanup** (if applicable — see Section A → Pre-existing orphans).

### Edge cases (must-know for LLMs)

1. **Patch updates require a cli_debrid restart** — the bind-mounted file is only imported at container start. Even if `os.path.getmtime` shows a fresh file, old code keeps running until restart.
2. **Heuristic returns `''` on unknown titles** → falls back to `default_category` (config). Account for this if the user wants a different fallback name.
3. **`_QUALITY_1080P` matches only an explicit `1080p` marker** — releases that don't state the resolution in the title go into the general bucket. Conservative by design.
4. **`_MUSIC_PATTERN` doesn't fire on generic audio markers** — only on unambiguous music indicators (FLAC, MP3, hi-res, discography). Extend if the false-negative rate is too high.
5. **Order inside `_detect_category_from_title`**: SHOW → MUSIC → MOVIE. `Some.Show.2024.S01E01.FLAC` classifies as SHOW (SxxExx wins).
6. **nzbdav cats must be configured in `api.categories`** — otherwise nzbdav rejects the submit (status=false). DB update plus container restart are mandatory.
7. **Plex section locations are not automated by this patch**. Direct Plex DB manipulation is possible but risky — see community caveats around the Plex section-locations PUT API (`value[]=ID` may be misread as a path → data loss). Manual UI changes are safer.
8. **Orphan DavItems** appear when SAB-history-delete is called without also cleaning the DavItems. nzbdav doesn't auto-clean these. Workaround: drop the branch from mergerfs (or accept the empty shells stay in the nzbdav DB).
9. **mergerfs `allow_other`** requires `user_allow_other` in `/etc/fuse.conf` for non-root host access. Without it only root or the container-internal user can read the union mount.
10. **mergerfs `-o` flag** is required for options. Without `-o` the options string is misread as a branch, so the mount falls back to defaults silently.

### Smoke-test commands (LLM-driven validation)

```bash
# 1. nzbdav cats:
curl -s "http://nzbdav:3000/api?mode=get_config&apikey=<key>" \
  | python3 -c "import json,sys; cats=[c['name'] for c in json.load(sys.stdin)['config']['categories']]; print('present' if all(c in cats for c in ['movies','shows','music','__unplayable__','movies_1080p_264','shows_1080p_264']) else 'MISSING')"

# 2. patch loaded inside cli_debrid:
docker exec cli_debrid grep -q "_detect_category_from_title" /app/usenet/nzbdav_client.py && echo "patch present"

# 3. config correct:
docker exec cli_debrid python3 -c "import json; c=json.load(open('/user/config/config.json')); assert c['Plex']['mounted_file_location']=='/mnt/debrid-usenet-union'; assert c['Usenet Provider']['download_folder']=='__unplayable__'; print('config OK')"

# 4. union mount populated:
docker exec cli_debrid sh -c '[ "$(ls /mnt/debrid-usenet-union | wc -l)" -gt 300 ] && echo "union OK"'

# 5. After the first grab — verify cat routing:
docker logs --since 5m cli_debrid 2>&1 | grep -E "NzbDAV.*NZB content submitted" | tail -5
# expected: cat must be 'movies'|'shows'|'music'|'*_1080p_264'|'__unplayable__', NOT 'cli_debrid'
```

### Companion files in this repo

```
cli_debrid_nzbdav_patch/
├── README.md            (this file)
├── PR_BODY.md           (original PR description, pre-extension)
├── diffs/               (raw diffs, useful when contributing back upstream)
└── app/                 (overlay tree, mirrors /app inside the cli_debrid container)
    ├── usenet/
    │   ├── nzbdav_client.py        <- title-heuristic core
    │   ├── __init__.py             <- provider factory
    │   └── decypharr_client.py     <- factory funcs only (class unchanged)
    ├── routes/                     <- UI routes for the nzbdav Settings tab
    ├── templates/                  <- HTML for Settings and Debrid Manager
    ├── static/                     <- JS for the scraper UI's nzbdav integration
    └── utilities/
        └── settings_schema.py      <- schema for nzbdav config keys
```

### PR-back to upstream

The patch can be contributed back to [Jauntiness's PR #425](https://github.com/godver3/cli_debrid/pull/425) — see `diffs/` for commit-ready patches. Suggested split:
- Title-heuristic + fallback rename as a follow-up commit on the PR.
- mergerfs-union as a separate guide (not a code component).

### Acknowledgement

This patch builds on Jauntiness's PR #425. Title-heuristic and zurg-naming mirror added 2026-05-27.

---

# Repair / health (provider-agnostic, 2026-05-28)

The NZB repair engine (`usenet/repair_engine.py`) previously issued raw HTTP to
Decypharr-only endpoints (`/api/repair/health`, `/api/repair/run`,
`/api/torrents`). On nzbdav those 404, so the whole auto-repair feature became a
silent no-op. It is now delegated to the provider client via the factory, so it
works for whichever backend is configured.

New client interface methods (on both `DecypharrClient` and `NzbdavClient`):
`fetch_broken_items()`, `get_health_summary()`, `trigger_health_scan()`,
`resolve_job_id()` — plus the existing `remove_nzb()`. `repair_engine.py` calls
these instead of building URLs itself.

How "broken" is determined per provider:
- **decypharr**: `/api/repair/health` entries with `status=broken` (rot detection).
- **nzbdav**: there is NO repair/health API. The only failure signal is history
  slots with `status=Failed` (`mode=history`). The client maps those to broken
  entries; repair then re-scrapes any that still map to a live cli-debrid item
  and purges the orphaned failed-history entries.

### Category scoping (shared-provider safety)

nzbdav history is shared with other SAB clients (Lidarr music, optionally
Radarr/Sonarr). cli-debrid repair can only re-acquire content **it** manages, so
`NzbdavClient` only considers its own categories for health/repair — it never
purges another app's entries. Resolution:

- include = `Usenet Provider.owned_categories` (comma list) if set, else the
  auto-default `movies, shows, movies_1080p_264, shows_1080p_264, __unplayable__`
  plus the configured `download_folder`.
- minus `Usenet Provider.exclude_categories` (comma list, optional).

`music` is intentionally excluded by default (cli-debrid manages video; Lidarr
owns music and self-heals its own grabs).

`task_repair_broken_nzbs` is disabled by default — repair only runs when you
enable the scheduled task or click the manual button in the Debrid Manager.

### NZB file naming + nzbdav (Plex mode)

Verified with `enable_nzb_naming`: the title heuristic classifies structured
names correctly (`Title (Year) - {imdb-…} - version - (original)` → movies /
shows / `*_1080p_264`), and nzbdav stores folder names verbatim so cli-debrid's
title→folder lookup resolves. Caveat: nzbdav appends a ` (2)`/`(3)` suffix on
name collisions; structured names are more deterministic, so re-grabs of the
same title can collide. The robust hardening (future) is to resolve the file
from the completed job's history `storage` path keyed by `nzo_id` rather than
fuzzy-matching by title.

---

# Migration doctor (`nzbdav_migrate.py`)

Standalone, stdlib-only preflight checker so you don't rediscover the setup
gotchas by hand. Read-only by default; works for any install.

```bash
python3 nzbdav_migrate.py                                  # auto-detect config, probe nzbdav
python3 nzbdav_migrate.py --url http://host:3000 --apikey KEY
python3 nzbdav_migrate.py --nzbdav-db /path/db.sqlite --fix # add missing categories
```

Checks: nzbdav reachability, which cli-debrid categories are missing from
nzbdav's `api.categories` (the #1 cause of grabs looping in Wanted), the
`Usenet Provider` config, mount visibility, and the Plex-mode subtitle note.
It does **not** bulk-replay your old Decypharr library — that proved unreliable
(trimmed .nzb files, double-nested folders, Plex purging "missing" items on a
hard mount-swap). Run nzbdav in parallel and let new grabs fill it instead.

---

# Setup & migration assistant (`nzbdav_setup.py`)

Standalone, stdlib-only assistant that scaffolds an NzbDAV backend for a **fresh**
install or a **migration from Decypharr**. It writes templated files into an output
directory and prints exact next steps — it never starts containers or edits your
live config unless you ask. Generic: prompts for your paths/credentials.

```bash
python3 nzbdav_setup.py wizard                 # interactive (fresh or migrate)
python3 nzbdav_setup.py generate \             # non-interactive file generation
    --nzbdav-host 192.168.1.50 --apikey KEY \
    --mount-base /srv/DUMB/mnt --out ./nzbdav-generated
python3 nzbdav_setup.py migrate-files \        # replay Decypharr .nzb → NzbDAV (best-effort)
    --nzbdav-host 192.168.1.50 --apikey KEY --decypharr-container decypharr
```

`generate` / `wizard` produce:
- `nzbdav/docker-compose.yml` — nzbdav + rclone sidecar (production read-ahead, WebDAV mount)
- `nzbdav/rclone.conf` — WebDAV remote (password auto-obscured if `rclone` is on PATH)
- `cli_debrid.usenet_provider.json` — the `Usenet Provider` config block to merge
  (`--write-config --cli-debrid-config <path>` merges it for you, with a backup)
- `SETUP_NOTES.md` — step-by-step: start stack → add categories → bind mount →
  set config → Plex library paths → verify with `nzbdav_migrate.py`

`migrate-files` is the **best-effort file move**: it copies each Decypharr `.nzb`
out of the container and replays it to NzbDAV via `mode=addfile` (resumable state
file, queue-depth throttle, configurable category map). It deliberately does NOT
hard-swap mounts. Heed the same caveat: many old `.nzb` files are gone from
Decypharr's store, so parallel-fill is usually the better migration path.
