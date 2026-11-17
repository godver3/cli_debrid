package engine

import (
	"runtime"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// CleanupResult is what RunCleanup did.
type CleanupResult struct {
	Expired    int   `json:"expired_segments"`
	Stray      int   `json:"stray_files_removed"`
	FreedBytes int64 `json:"freed_bytes"`
	CacheBytes int64 `json:"cache_bytes_after"`
	IdleClosed bool  `json:"idle_connections_closed"`
}

// RunCleanup runs the periodic maintenance now (the Mount page's "Run
// cleanup"): disk cache size limit and expiry, stray cache files, idle
// usenet connections.
func (e *Engine) RunCleanup() CleanupResult {
	var r CleanupResult
	if e.Disk != nil {
		r.Expired, r.Stray, r.FreedBytes = e.Disk.Cleanup(time.Duration(e.cfg.CacheExpiry) * time.Second)
		_, r.CacheBytes = e.Disk.Usage()
	}
	e.Client.CloseIdle(time.Duration(e.cfg.IdleTimeout) * time.Second)
	r.IdleClosed = true
	e.log.Info("cleanup run", "expired", r.Expired, "stray", r.Stray, "freed", r.FreedBytes, "cache_bytes", r.CacheBytes)
	return r
}

// PurgeResult is what PurgeCache did.
type PurgeResult struct {
	DiskSegments int   `json:"disk_segments_removed"`
	DiskBytes    int64 `json:"disk_bytes_freed"`
	MemSegments  int   `json:"memory_segments_dropped"`
	MemBytes     int64 `json:"memory_bytes_freed"`
}

// PurgeCache empties the memory and disk caches (the Mount page's "Purge
// cache"). Open files keep working; their next reads fetch again.
func (e *Engine) PurgeCache() PurgeResult {
	var r PurgeResult
	r.MemSegments, r.MemBytes = e.Cache.Purge()
	if e.Disk != nil {
		r.DiskSegments, r.DiskBytes = e.Disk.Purge()
	}
	e.log.Info("cache purged", "disk_segments", r.DiskSegments, "disk_bytes", r.DiskBytes,
		"memory_segments", r.MemSegments, "memory_bytes", r.MemBytes)
	return r
}

// Overview is the extra status the Mount page shows (cli_mount's stats).
type Overview struct {
	UptimeSeconds int64            `json:"uptime_seconds"`
	Content       map[string]int64 `json:"content"` // ready jobs, files, bytes; usenet / debrid split
	Health        map[string]int   `json:"health"`  // healthy, broken, unknown, unchecked
	Streams       map[string]int64 `json:"streams"` // open files, bytes/s
	Runtime       map[string]int64 `json:"runtime"` // goroutines, heap, sys
	Protection    ProtectionStatus `json:"plex_protection"`
}

func (e *Engine) Overview() Overview {
	o := Overview{UptimeSeconds: int64(time.Since(e.started).Seconds()),
		Content: map[string]int64{}, Health: map[string]int{}, Streams: map[string]int64{}, Runtime: map[string]int64{}}
	for _, j := range e.Store.List() {
		if j.Status != store.Ready {
			continue
		}
		kind := "usenet"
		if j.Protocol == ProtocolDebrid {
			kind = "debrid"
		}
		o.Content["jobs"]++
		o.Content[kind+"_jobs"]++
		o.Content["bytes"] += j.Size
		o.Content[kind+"_bytes"] += j.Size
		if j.Protocol == ProtocolDebrid {
			continue
		}
		switch {
		case j.Health == nil:
			o.Health["unchecked"]++
		case j.Health.Status == health.Broken:
			o.Health["broken"]++
		default:
			o.Health[j.Health.Status]++
		}
	}
	for _, f := range e.Folders(AllFolder) {
		o.Content["files"] += int64(len(f.Entries))
	}
	for _, s := range e.Streams() {
		o.Streams["open"]++
		o.Streams["rate_bytes"] += s.RateBytes
	}
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	o.Runtime["goroutines"] = int64(runtime.NumGoroutine())
	o.Runtime["heap_bytes"] = int64(m.HeapAlloc)
	o.Runtime["sys_bytes"] = int64(m.Sys)
	o.Protection = e.Protection()
	return o
}
