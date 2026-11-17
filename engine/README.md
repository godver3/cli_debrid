# clid-engine

cli_debrid's optional built-in mount: imports NZBs, serves their files
over FUSE and exposes a localhost API. Nothing in cli_debrid uses it yet;
the external backends (cli_mount, NzbDAV, Zurg) are unaffected.

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

Mount layout (same as cli_mount): `<mount>/__all__/<job>/<file>` and
`<mount>/nzbs/<job>/<file>`.

API: see `internal/api/api.go`. Third-party code: see `NOTICE.md`.
