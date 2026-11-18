# clid-engine

cli_debrid's built-in mount: imports NZBs, streams them and the user's
Real-Debrid / TorBox torrents over FUSE (WinFsp on Windows), and exposes a
localhost API. It is the `builtin` usenet backend (the default for new
installs); cli_mount, NzbDAV and Zurg stay available as external backends.

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
`<mount>/__bad__/` and any virtual folders.

API: see `internal/api/api.go`. Third-party code: see `NOTICE.md`.
