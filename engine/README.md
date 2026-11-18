# clid-engine

cli_debrid's built-in mount: imports NZBs, streams them and the user's
debrid torrents (all six providers cli_debrid supports; cli_debrid hands
out the links) over FUSE (WinFsp on Windows) and optionally a read-only
WebDAV share, and exposes a localhost API. It is the `builtin` usenet
backend and can also run for debrid alone (no news server); it's the
default for new installs. cli_mount, NzbDAV and Zurg stay available as
external backends.

cli_debrid runs it under supervisord (`mount_engine`), writes its
`engine.json` from the *Built-in Mount* settings
(`utilities/builtin_engine.py`) and talks to it through
`usenet/builtin_client.py`. Users manage it on the Mount page.

```
go test ./...                 # FUSE tests skip without /dev/fuse
go build ./cmd/clid-engine
clid-engine --config engine.json
```

Example config:

```json
{
  "data_dir": "/config/engine",
  "cache_dir": "/cache/engine",
  "cache_bytes": 53687091200,
  "mount_path": "/mnt/clid",
  "listen": "127.0.0.1:8283",
  "token": "change-me",
  "read_ahead": 8,
  "import_workers": 2,
  "allow_other": true,
  "webdav_listen": "0.0.0.0:8284",
  "webdav_user": "u",
  "webdav_password": "p",
  "providers": [
    {"name": "main", "host": "news.example.com", "port": 563, "tls": true,
     "username": "u", "password": "p", "max_conns": 20},
    {"name": "fill", "host": "fill.example.com", "port": 563, "tls": true,
     "username": "u", "password": "p", "max_conns": 5, "backup": true}
  ]
}
```

Mount layout (same as cli_mount): `<mount>/__all__/<job>/<file>`,
`<mount>/nzbs/`, `<mount>/torrents/`, one folder per debrid provider,
`<mount>/__bad__/` and any virtual folders. The mount is read-only except
for subtitle sidecars (`.srt`, `.ass`...) written into a job folder; they
are stored under `<data_dir>/sidecars/<job id>/` and go with the job.
`providers` may be empty (debrid only): fetches then fail inconclusively,
never as proof of a missing article.

API: see `internal/api/api.go`. Third-party code: see `NOTICE.md`.
