// Package engine ties the parts together: job store, NNTP client, caches,
// the import queue, and the folder tree the mount and API expose.
package engine

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"path/filepath"
	"regexp"
	"runtime/debug"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/diskcache"
	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
	"github.com/godver3/cli_debrid/engine/internal/usage"
)

// Top-level folders, matching cli_mount so existing symlinks keep resolving
// after a migration: every job under __all__, usenet jobs also under nzbs,
// debrid jobs also under torrents and under a folder named after their
// provider (realdebrid, torbox...), and jobs with a proven-broken file also
// under __bad__.
const (
	AllFolder     = "__all__"
	BadFolder     = "__bad__"
	TorrentFolder = "torrents"
	NZBFolder     = "nzbs"
)

// fixedTops are always listed, even when empty, like cli_mount.
var fixedTops = []string{AllFolder, BadFolder, TorrentFolder, NZBFolder}

// providerFolder names the per-provider top folder of a debrid job.
func providerFolder(j store.Job) string {
	p := j.Provider
	if p == "" { // registered before the provider was recorded: "<key>-<torrent id>"
		if i := strings.Index(j.ID, "-"); i > 0 {
			p = j.ID[:i]
		}
	}
	switch strings.ToLower(p) {
	case "", "debrid":
		return ""
	case "rd":
		return "realdebrid"
	case "tb":
		return "torbox"
	case "ad":
		return "alldebrid"
	case "pm":
		return "premiumize"
	case "dl":
		return "debridlink"
	case "hw":
		return "highway"
	}
	p = strings.ToLower(cleanFolderName(p))
	for _, t := range fixedTops {
		if p == t {
			return ""
		}
	}
	return p
}

// Config is the engine's configuration.
type Config struct {
	DataDir       string          `json:"data_dir"`
	CacheDir      string          `json:"cache_dir"`
	CacheBytes    int64           `json:"cache_bytes"`
	MemCacheBytes int64           `json:"mem_cache_bytes"`
	MountPath     string          `json:"mount_path"`
	Listen        string          `json:"listen"`
	Token         string          `json:"token"`
	ReadAhead     int             `json:"read_ahead"`             // segments prefetched after a live read
	ReadAheadMB   int             `json:"read_ahead_mb"`          // or: megabytes ahead (wins when set)
	FileConns     int             `json:"max_conns_per_file"`     // prefetches at once per open file (0 = no cap)
	ImportTimeout int             `json:"import_timeout_seconds"` // per import; a timeout is retryable, never final
	ImportSample  float64         `json:"import_sample_percent"`  // % of articles checked before a job is ready (0 = off)
	HealthSample  float64         `json:"health_sample_percent"`  // % checked by health checks (0 = 10%, as cli_mount)
	MediaProbe    bool            `json:"media_probe"`            // health checks also run ffprobe (cli_mount's media_probe_usenet)
	FFprobe       string          `json:"ffprobe_path"`           // ffprobe binary (default: "ffprobe" on PATH)
	IdleTimeout   int             `json:"idle_timeout_seconds"`   // close NNTP connections unused this long (default 300)
	CacheExpiry   int             `json:"cache_expiry_seconds"`   // drop disk cache entries unused this long (0 = only by size)
	LinkCallback  string          `json:"link_callback"`          // cli_debrid URL that hands out debrid download links
	DebridChunkMB int             `json:"debrid_chunk_mb"`        // debrid HTTP range size (0 = 4 MB)
	DebridConns   int             `json:"debrid_conns_per_file"`  // parallel debrid ranges per file (0 = 4)
	DebridAheadMB int             `json:"debrid_read_ahead_mb"`   // debrid read-ahead (0 = read_ahead_mb)
	DebridUA      string          `json:"debrid_user_agent"`      // User-Agent for debrid downloads ("" = default)
	DebridProxy   string          `json:"debrid_proxy"`           // proxy URL for debrid downloads ("" = none / env)
	PreCacheMB    int             `json:"precache_mb"`            // on open, fetch this much of the start and end (0 = off)
	CacheCleanup  int             `json:"cache_cleanup_seconds"`  // how often the expiry runs (default 60)
	ImportWorkers int             `json:"import_workers"`         // jobs imported at once
	HealthWorkers int             `json:"health_workers"`         // jobs health-checked at once
	Parallel      int             `json:"parallel"`               // probes per import
	Providers     []nntp.Provider `json:"providers"`
	// PrimaryLoadMode: "fill" (default) uses the first primary until it is
	// full; "balanced" spreads requests over every primary by load.
	PrimaryLoadMode string `json:"primary_load_mode"`
	// PlexProtection: the playback reserve is only for files Plex reports
	// playing (cli_debrid posts them to /protected); other mount reads
	// (scanner, thumbnails, ffprobe) can't take it while one plays.
	PlexProtection bool `json:"plex_protection"`
	// AllowedExt: file extensions the mount shows (empty = all). Other
	// files stay in the job (health, repair) but are not listed.
	AllowedExt []string `json:"allowed_extensions"`
	// DeobfuscateMode renames new imports' video files: "" off, "index",
	// "season_ep" (cli_mount's modes).
	DeobfuscateMode string `json:"deobfuscate_mode"`
	// VirtualFolders: extra top folders of the jobs matching their filters.
	VirtualFolders map[string]VirtualFolder `json:"virtual_folders"`
	// MemoryLimitMB is the Go runtime's soft memory limit. 0 = the memory
	// cache plus 768 MB; -1 = no limit. Without it the default GC lets the
	// heap grow to twice the live data, and the memory cache alone is live:
	// found live, 2.7 GB of heap for a 1 GB cache.
	MemoryLimitMB int `json:"memory_limit_mb"`
	// RetryBackoff: waits before re-importing after an unproven failure
	// (default 1m, 5m, 15m); once used up the job fails, still retryable.
	RetryBackoff []time.Duration `json:"-"`
}

// memoryLimit returns the soft memory limit in bytes (0 = none).
func (c Config) memoryLimit() int64 {
	switch {
	case c.MemoryLimitMB < 0:
		return 0
	case c.MemoryLimitMB > 0:
		return int64(c.MemoryLimitMB) << 20
	}
	return c.MemCacheBytes + 768<<20
}

func (c *Config) defaults() {
	if c.MemCacheBytes <= 0 {
		// Match Clyde's generated config and cli_mount's buffer default.
		c.MemCacheBytes = 512 << 20
	}
	if c.ReadAhead <= 0 {
		c.ReadAhead = 8
	}

	if c.ImportWorkers <= 0 {
		c.ImportWorkers = 8
	}
	if c.HealthWorkers <= 0 {
		c.HealthWorkers = 1
	}
	if c.Parallel <= 0 {
		c.Parallel = 8
	}
	if c.Listen == "" {
		c.Listen = "127.0.0.1:8283"
	}
	if c.IdleTimeout <= 0 {
		c.IdleTimeout = 300
	}
	if c.RetryBackoff == nil {
		c.RetryBackoff = []time.Duration{time.Minute, 5 * time.Minute, 15 * time.Minute}
	}
	if c.CacheCleanup <= 0 {
		c.CacheCleanup = 60
	}
}

// remoteOptions: debrid streaming settings (a bad proxy URL is logged and
// ignored, so streaming still works without it).
func (c Config) remoteOptions(log *slog.Logger) stream.RemoteOptions {
	o := stream.RemoteOptions{ChunkBytes: int64(c.DebridChunkMB) << 20, MaxConns: c.DebridConns,
		ReadAheadBytes: int64(c.DebridAheadMB) << 20, UserAgent: c.DebridUA}
	if c.DebridProxy != "" {
		u, err := url.Parse(c.DebridProxy)
		if err != nil || u.Host == "" {
			log.Warn("debrid proxy ignored: not a URL", "proxy", c.DebridProxy)
		} else {
			o.Client = stream.NewRemoteClient(http.ProxyURL(u))
		}
	}
	return o
}

func (c Config) readAhead() stream.ReadAhead {
	return stream.ReadAhead{Segments: c.ReadAhead, Bytes: int64(c.ReadAheadMB) << 20, MaxConns: c.FileConns}
}

// DefaultHealthSample is cli_mount's repair availability sample (10% of
// each file's articles, no cap), so a check proves as much as cli_mount's.
const DefaultHealthSample = 10

// healthOptions turns a sample percentage into health.Options (never
// capped per file, like cli_mount).
func healthOptions(percent float64, full bool) health.Options {
	if full || percent >= 100 {
		return health.Options{Full: true}
	}
	if percent <= 0 {
		percent = DefaultHealthSample
	}
	return health.Options{SampleRate: percent / 100, Max: 1 << 30}
}

// maintain closes idle NNTP connections and expires the disk cache.
func (e *Engine) maintain() {
	defer e.wg.Done()
	idle := time.Duration(e.cfg.IdleTimeout) * time.Second
	expiry := time.Duration(e.cfg.CacheExpiry) * time.Second
	t := time.NewTicker(min(idle, time.Duration(e.cfg.CacheCleanup)*time.Second))
	defer t.Stop()
	lastExpire := time.Now()
	for {
		select {
		case <-e.ctx.Done():
			return
		case <-t.C:
			e.Client.CloseIdle(idle)
			e.Client.Warm(e.ctx)
			e.trimOpen(time.Now())
			if e.Disk != nil && expiry > 0 && time.Since(lastExpire) >= time.Duration(e.cfg.CacheCleanup)*time.Second {
				if n := e.Disk.Expire(expiry); n > 0 {
					e.log.Debug("disk cache expired", "segments", n)
				}
				lastExpire = time.Now()
			}
		}
	}
}

// trackUsage folds the live byte counters into the per-day usage file
// every 30 seconds, and once more on shutdown.
func (e *Engine) trackUsage() {
	defer e.wg.Done()
	t := time.NewTicker(30 * time.Second)
	defer t.Stop()
	sample := func() {
		pools, _, _, _ := e.Client.Stats()
		for _, p := range pools {
			e.Usage.Observe("usenet/"+p.Name, p.Bytes)
		}
		for _, d := range stream.RemoteStats() {
			e.Usage.Observe("debrid/"+d.Provider, d.Bytes)
		}
		if err := e.Usage.Flush(); err != nil {
			e.log.Warn("could not save provider usage", "err", err)
		}
	}
	for {
		select {
		case <-e.ctx.Done():
			sample()
			return
		case <-t.C:
			sample()
		}
	}
}

// Engine is safe for concurrent use.
type Engine struct {
	cfg    Config
	log    *slog.Logger
	Client *nntp.Client
	// Usage is bytes per day per usenet server ("usenet/<name>") and debrid
	// provider ("debrid/<rd|tb|...>"), kept 30 days on disk.
	Usage *usage.Tracker
	Cache *stream.Cache
	Disk  *diskcache.Cache
	Store *store.Store

	queue  chan string
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	mu   sync.Mutex
	open map[string]*openJob // job id -> readers (built lazily, dropped when idle)

	treeMu sync.RWMutex
	// rebuildMu serializes rebuildTree, so a slower rebuild can't install
	// an older tree over a newer one.
	rebuildMu sync.Mutex
	protect   protection // Plex playback protection
	// importsActive: imports running right now. Plex protection only applies
	// while an import is queued or running (cli_mount's background-contention
	// rule); with no imports nothing competes with playback.
	importsActive atomic.Int32
	started       time.Time
	vfolders      []namedFolder                 // compiled VirtualFolders, by name
	tree          map[string]map[string]*Folder // top folder -> folder name -> job folder
	tops          []string                      // top folders in listing order

	healthSem chan struct{}
	prober    mediaProber
	streams   streamTable
	links     *callbackLinks
	refNames  sync.Map // debrid ref -> "job/file", for log lines
	stopReq   chan struct{}
	stopOnce  sync.Once
	runMu     sync.Mutex
	run       *HealthRun
	runCancel context.CancelFunc
}

// Folder is one job's folder in the tree.
type Folder struct {
	JobID   string
	Name    string
	Entries []job.Entry // names unique within the folder
	ModTime time.Time
}

// New opens the store and caches and starts the import workers.
func New(cfg Config, log *slog.Logger) (*Engine, error) {
	cfg.defaults()
	if log == nil {
		log = slog.Default()
	}
	if lim := cfg.memoryLimit(); lim > 0 {
		debug.SetMemoryLimit(lim)
		log.Info("memory limit", "mb", lim>>20)
	}
	st, err := store.Open(cfg.DataDir)
	if err != nil {
		return nil, fmt.Errorf("engine: store: %w", err)
	}
	client, err := nntp.NewClient(cfg.Providers)
	if err != nil {
		return nil, fmt.Errorf("engine: %w", err)
	}
	client.SetLoadMode(nntp.ParseLoadMode(cfg.PrimaryLoadMode))
	log.Info("usenet primary load mode", "mode", client.LoadMode().String())
	e := &Engine{cfg: cfg, log: log, Client: client, Store: st,
		Cache: stream.NewCache(cfg.MemCacheBytes),
		queue: make(chan string, 4096), open: map[string]*openJob{},
		healthSem: make(chan struct{}, cfg.HealthWorkers), stopReq: make(chan struct{})}
	if cfg.CacheDir != "" && cfg.CacheBytes > 0 {
		if e.Disk, err = diskcache.Open(cfg.CacheDir, cfg.CacheBytes); err != nil {
			return nil, fmt.Errorf("engine: disk cache: %w", err)
		}
		e.Cache.WithDisk(e.Disk)
	}
	e.started = time.Now()
	e.vfolders = compileVirtualFolders(cfg.VirtualFolders, log)
	e.ctx, e.cancel = context.WithCancel(context.Background())
	for i := 0; i < cfg.ImportWorkers; i++ {
		e.wg.Add(1)
		go e.worker()
	}
	e.links = newCallbackLinks(cfg.LinkCallback, cfg.Token, log.With("component", "debrid"))
	e.links.opts = cfg.remoteOptions(log)
	e.links.names = e.refName
	e.prober.e = e
	e.Client.SetLogger(log.With("component", "nntp"))
	e.Usage = usage.Open(filepath.Join(cfg.DataDir, "provider_usage.json"))
	for _, d := range stream.RemoteStats() { // process-wide counters
		e.Usage.Baseline("debrid/"+d.Provider, d.Bytes)
	}
	e.wg.Add(2)
	go e.maintain()
	go e.trackUsage()
	e.Client.Warm(e.ctx)
	for _, j := range st.List() {
		if j.Status == store.Queued {
			e.queue <- j.ID
		}
		// A debrid job whose file map can't be read is dropped: the account
		// mirror adds the torrent back with fresh file references.
		if _, res, _ := st.Get(j.ID); j.Status == store.Ready && j.Protocol == ProtocolDebrid && res == nil {
			log.Warn("debrid job's file map is unreadable; removing it so the account sync re-adds it", "job", j.ID, "name", j.Name)
			_ = st.Delete(j.ID)
		}
	}
	e.rebuildTree()
	return e, nil
}

// Logger is the engine's logger.
func (e *Engine) Logger() *slog.Logger { return e.log }

// Config returns the effective configuration.
func (e *Engine) Config() Config { return e.cfg }

// Close stops workers and connections.
func (e *Engine) Close() {
	e.cancel()
	e.wg.Wait()
	e.Client.Close()
	e.prober.close()
	if err := e.Store.Close(); err != nil {
		e.log.Warn("saving job index", "err", err)
	}
	if e.Disk != nil {
		e.Disk.Flush()
	}
}

// NewID returns a random job id.
func NewID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%s-%s-%s-%s-%s", hex.EncodeToString(b[0:4]), hex.EncodeToString(b[4:6]),
		hex.EncodeToString(b[6:8]), hex.EncodeToString(b[8:10]), hex.EncodeToString(b[10:]))
}

// ErrBadNZB means the submitted data is not a usable NZB.
var ErrBadNZB = errors.New("engine: invalid NZB")

// ErrNoAllowedFiles: the release imported but none of its files has an
// allowed extension, so the mount would show nothing.
var ErrNoAllowedFiles = errors.New("engine: no file with an allowed extension")

// Submit stores an NZB and queues its import. id may be given (migration
// keeps cli_mount's job ids); otherwise one is generated.
func (e *Engine) Submit(data []byte, name, category, id string) (store.Job, error) {
	return e.SubmitPassword(data, name, category, id, "")
}

// SubmitPassword is Submit with an archive password. A password may also
// come as "Name{{password}}" (SABnzbd's convention) or in the NZB's own
// <meta type="password">. It is kept in the stored NZB's head, so retries,
// health checks and a copy back to cli_mount all see it, and never in the
// job record or folder name.
func (e *Engine) SubmitPassword(data []byte, name, category, id, password string) (store.Job, error) {
	name, fromName := splitNamePassword(name)
	if password == "" {
		password = fromName
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return store.Job{}, fmt.Errorf("%w: %v", ErrBadNZB, err)
	}
	if password != "" && job.NZBPassword(parsed) == "" {
		if withPw, ok := withPasswordMeta(data, password); ok {
			data = withPw
		} else {
			e.log.Warn("job received: could not store the archive password in the NZB", "name", name)
		}
	}
	if strings.TrimSpace(name) == "" {
		name = parsed.Meta["title"]
	}
	if strings.TrimSpace(name) == "" {
		name = strings.TrimSuffix(parsed.Files[0].Name, ".par2")
	}
	if id == "" {
		id = NewID()
	}
	j, err := e.Store.Create(store.Job{ID: id, Name: cleanFolderName(name), Category: category}, data)
	if err != nil {
		return store.Job{}, err
	}
	e.log.Info("job received", "job", j.ID, "name", j.Name, "category", category, "files", len(parsed.Files), "queued", len(e.queue))
	e.queue <- j.ID
	return j, nil
}

func (e *Engine) worker() {
	defer e.wg.Done()
	for {
		select {
		case <-e.ctx.Done():
			return
		case id := <-e.queue:
			e.importJob(id)
		}
	}
}

const importTimeoutGrace = time.Second

func (e *Engine) importJob(id string) {
	started, ctx, finish, err := e.Store.BeginImport(e.ctx, id)
	if err != nil {
		return
	}
	defer finish()
	e.importsActive.Add(1)
	defer e.importsActive.Add(-1)
	e.log.Info("import started", "job", id, "name", started.Name, "attempt", started.Attempts+1,
		"since_received", time.Since(started.CreatedAt).Round(time.Second))
	start := time.Now()
	res, err := e.runImport(ctx, started)
	e.Store.CommitImport(ctx, func() {
		if err != nil {
			if res != nil && len(res.Failures) > 0 && !strings.Contains(err.Error(), res.Failures[0].Name) {
				err = fmt.Errorf("%w (%s)", err, failureSummary(res.Failures))
			}
			e.logBuild(id, res, true)
			e.failImport(id, err)
			return
		}
		if _, err := e.publish(id, res); err != nil {
			e.failImport(id, err)
			return
		}
		e.log.Info("imported", "job", id, "entries", len(res.Entries), "failures", len(res.Failures), "took", time.Since(start))
		e.logBuild(id, res, false)
	})
}

// runImport keeps the build separate from publication. If the build ignores
// cancellation, its late result can be discarded and the worker can move on.
func (e *Engine) runImport(parent context.Context, started store.Job) (*job.Result, error) {
	ctx := parent
	timeout := time.Duration(e.cfg.ImportTimeout) * time.Second
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(parent, timeout)
		defer cancel()
	}
	type outcome struct {
		result *job.Result
		err    error
	}
	done := make(chan outcome, 1)
	go func() {
		res, err := e.buildImport(ctx, started)
		done <- outcome{res, err}
	}()
	var res *job.Result
	select {
	case out := <-done:
		if ctx.Err() == nil {
			return out.result, out.err
		}
		res = out.result
	case <-ctx.Done():
		if parent.Err() != nil {
			return nil, parent.Err()
		}
		deadline, _ := ctx.Deadline()
		grace := time.NewTimer(time.Until(deadline.Add(importTimeoutGrace)))
		defer grace.Stop()
		select {
		case out := <-done:
			res = out.result
		case <-grace.C:
		case <-parent.Done():
			return nil, parent.Err()
		}
	}
	if err := parent.Err(); err != nil {
		return nil, err
	}
	return res, fmt.Errorf("%w: import took longer than %ds", job.ErrInconclusive, e.cfg.ImportTimeout)
}

func (e *Engine) buildImport(ctx context.Context, started store.Job) (*job.Result, error) {
	data, err := e.Store.NZB(started.ID)
	if err != nil {
		return nil, fmt.Errorf("%w: reading stored NZB: %v", job.ErrInconclusive, err)
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBadNZB, err)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	res, err := job.Build(ctx, e.Client, e.Cache, parsed, job.Options{Parallel: e.cfg.Parallel})
	if err != nil {
		return res, err
	}
	if err := ctx.Err(); err != nil {
		return res, err
	}
	if e.cfg.DeobfuscateMode != "" {
		if j, _, err := e.Store.Get(started.ID); err == nil {
			if n := Deobfuscate(res, e.cfg.DeobfuscateMode, j.Name); n > 0 {
				e.log.Debug("import: renamed obfuscated files", "job", j.ID, "files", n, "mode", e.cfg.DeobfuscateMode)
			}
		}
	}
	if e.cfg.ImportSample > 0 {
		if err := e.importSample(ctx, parsed, res); err != nil {
			return res, err
		}
	}
	if e.HiddenByExtension(res) {
		return res, fmt.Errorf("%w (has %s; allowed: %s)", ErrNoAllowedFiles,
			strings.Join(entryExts(res.Entries), ", "), strings.Join(e.cfg.AllowedExt, ", "))
	}
	return res, nil
}

func (e *Engine) failImport(id string, cause error) {
	// Dead-marking needs proof. A job waits in the queue during backoff
	// and remains retryable even after exhausting its automatic retries.
	retry := errors.Is(cause, job.ErrInconclusive)
	j, err := e.Store.Update(id, func(j *store.Job) {
		j.Error, j.Retryable = cause.Error(), retry
		j.Status = store.Failed
		if retry && j.Attempts < len(e.cfg.RetryBackoff) {
			j.Status = store.Queued
			j.Attempts++
		}
	})
	if err != nil {
		e.log.Error("recording import failure", "job", id, "err", err, "cause", cause)
		return
	}
	if j.Status == store.Queued {
		wait := e.cfg.RetryBackoff[j.Attempts-1]
		e.log.Warn("import inconclusive, retrying", "job", id, "err", cause, "attempt", j.Attempts, "in", wait)
		time.AfterFunc(wait, func() { e.requeue(j) })
		return
	}
	e.log.Warn("import failed", "job", id, "name", j.Name, "err", cause, "retryable", retry)
}

// requeue puts the waiting attempt back on the import queue if it is still current.
func (e *Engine) requeue(waiting store.Job) {
	if e.ctx.Err() != nil {
		return
	}
	if j, _, err := e.Store.Get(waiting.ID); err == nil && j.Status == store.Queued &&
		j.CreatedAt.Equal(waiting.CreatedAt) && j.Attempts == waiting.Attempts {
		select {
		case e.queue <- j.ID:
		case <-e.ctx.Done():
		}
	}
}

// PreCache warms the start and end of a file that was just opened (players
// read the header, then the index at the end) in parallel, at prefetch
// priority. The player's own first reads join these fetches instead of
// starting their own (the cache collapses duplicates).
func (e *Engine) PreCache(jobID string, en job.Entry) {
	n := int64(e.cfg.PreCacheMB) << 20
	if n <= 0 || en.Size <= 0 {
		return
	}
	files, err := e.Files(jobID)
	if err != nil {
		return
	}
	r := files.Entry(en)
	if files.Remote(en) {
		// Debrid: prefetch-priority reads fetch whole cache chunks (8 MB
		// each end, for every scanner open). Start the playback streams
		// at the head and the tail instead, n bytes deep; the player's
		// reads join them.
		ctx, cancel := context.WithTimeout(e.ctx, 2*time.Minute)
		defer cancel()
		hctx := stream.WithReadHint(ctx, stream.ReadHint{Ahead: n})
		var wg sync.WaitGroup
		for _, off := range []int64{0, max(en.Size-n, 0)} {
			wg.Add(1)
			go func(off int64) {
				defer wg.Done()
				defer func() {
					if p := recover(); p != nil {
						e.log.Error("pre-cache crashed", "job", jobID, "file", en.Name, "panic", p)
					}
				}()
				buf := make([]byte, min(64<<10, en.Size-off))
				r.ReadAtCtx(hctx, nntp.Read, buf, off) // errors surface on the real read
			}(off)
		}
		wg.Wait()
		return
	}
	const chunk = 512 << 10
	var ranges [][2]int64
	add := func(from, to int64) {
		for off := max(from, 0); off < min(to, en.Size); off += chunk {
			ranges = append(ranges, [2]int64{off, min(chunk, en.Size-off)})
		}
	}
	add(0, n)
	if en.Size > 2*n {
		add(en.Size-n, en.Size)
	} else {
		add(n, en.Size)
	}
	ctx, cancel := context.WithTimeout(e.ctx, 2*time.Minute)
	defer cancel()
	var wg sync.WaitGroup
	for _, rg := range ranges {
		wg.Add(1)
		go func(off, l int64) {
			defer wg.Done()
			defer func() {
				if p := recover(); p != nil {
					e.log.Error("pre-cache crashed", "job", jobID, "file", en.Name, "panic", p)
				}
			}()
			buf := make([]byte, l)
			r.ReadAtCtx(ctx, nntp.Prefetch, buf, off) // errors surface on the real read
		}(rg[0], rg[1])
	}
	wg.Wait()
}

// ErrUnavailable: the import-time availability sample proved articles of
// the main file missing on every provider.
var ErrUnavailable = errors.New("engine: articles missing on every provider")

// importSample checks a share of the largest entry's articles before the
// job becomes ready (cli_mount's "NZB import availability sample"). Only a
// proven miss fails the import; an inconclusive check passes.
func (e *Engine) importSample(ctx context.Context, parsed *nzb.NZB, res *job.Result) error {
	if len(res.Entries) == 0 {
		return nil
	}
	main := *res
	main.Entries = res.Entries[:1] // sorted largest first
	rep, err := health.Check(ctx, e.Client, parsed, &main, healthOptions(e.cfg.ImportSample, false))
	if err != nil {
		return nil
	}
	if b := rep.Broken(); len(b) > 0 {
		return fmt.Errorf("%w: %s (article %s)", ErrUnavailable, b[0].Name, b[0].Segment)
	}
	return nil
}

// failureSummary lists up to three per-file failures for the job error, so
// a failed import says why without digging through logs.
// logBuild logs what an import found: files left out (always, as warnings)
// and, on failure or at debug level, the notes on every set and skip.
func (e *Engine) logBuild(id string, res *job.Result, failed bool) {
	if res == nil {
		return
	}
	for _, f := range res.Failures {
		e.log.Warn("import: file not streamable", "job", id, "file", f.Name, "size", f.Size, "err", f.Error)
	}
	for _, n := range res.Notes {
		if failed {
			e.log.Warn("import: "+n, "job", id)
		} else {
			e.log.Debug("import: "+n, "job", id)
		}
	}
}

func failureSummary(fs []job.Failure) string {
	var parts []string
	for i, f := range fs {
		if i == 3 {
			parts = append(parts, fmt.Sprintf("and %d more", len(fs)-3))
			break
		}
		parts = append(parts, f.Name+": "+f.Error)
	}
	return strings.Join(parts, "; ")
}

// Delete removes a job (and its folder) for good.
func (e *Engine) Delete(id string) error {
	if err := e.Store.Delete(id); err != nil {
		return err
	}
	e.mu.Lock()
	delete(e.open, id)
	e.mu.Unlock()
	e.deleteSidecars(id)
	e.rebuildTree()
	return nil
}

// Rename changes a job's folder name.
func (e *Engine) Rename(id, name string) (store.Job, error) {
	name = cleanFolderName(name)
	if name == "" {
		return store.Job{}, fmt.Errorf("engine: empty name")
	}
	j, err := e.Store.Update(id, func(j *store.Job) { j.Name = name })
	if err == nil {
		e.rebuildTree()
	}
	return j, err
}

// Files returns readers for a ready job, building them on first use.
// Built readers hold the job's parsed NZB (every article id), so they are
// kept only while in use: see trimOpen.
func (e *Engine) Files(id string) (*job.Files, error) {
	e.mu.Lock()
	if oj, ok := e.open[id]; ok {
		oj.used = time.Now()
		e.mu.Unlock()
		return oj.fs, nil
	}
	e.mu.Unlock()
	j, res, err := e.Store.Full(id) // segment maps load here, not at start
	if err != nil {
		return nil, err
	}
	if j.Status != store.Ready || res == nil {
		return nil, fmt.Errorf("engine: job %s is %s", id, j.Status)
	}
	data, err := e.Store.NZB(id)
	if err != nil {
		return nil, err
	}
	var parsed *nzb.NZB
	if j.Protocol != ProtocolDebrid {
		if parsed, err = nzb.Parse(bytes.NewReader(data)); err != nil {
			return nil, err
		}
	}
	fs, err := job.Open(res, parsed, e.Client, e.Cache, e.cfg.readAhead(), e.links)
	if err != nil {
		return nil, err
	}
	e.nameRefs(j.Name, res)
	e.mu.Lock()
	if prev, ok := e.open[id]; ok {
		prev.used = time.Now()
		e.mu.Unlock()
		return prev.fs, nil
	}
	e.open[id] = &openJob{fs: fs, used: time.Now()}
	over := len(e.open) > maxOpenJobs
	e.mu.Unlock()
	if over {
		e.trimOpen(time.Now())
	}
	return fs, nil
}

// nameRefs records the "job/file" name of each debrid source so link log
// lines say which file they are for.
func (e *Engine) nameRefs(jobName string, res *job.Result) {
	for _, s := range res.Sources {
		if s.Remote != "" {
			e.refNames.Store(s.Remote, jobName+"/"+s.Name)
		}
	}
}

func (e *Engine) refName(ref string) string {
	if v, ok := e.refNames.Load(ref); ok {
		return v.(string)
	}
	return ""
}

// openJob is a job's built readers and when they were last asked for.
type openJob struct {
	fs   *job.Files
	used time.Time
}

const (
	// openJobIdle: built readers not asked for this long are dropped (a
	// Plex scan or health sweep opens every job once; without this the
	// parsed NZBs of the whole library would stay in memory).
	openJobIdle = 2 * time.Minute
	// maxOpenJobs caps built readers of jobs with no open file.
	maxOpenJobs = 64
)

// trimOpen drops built readers of jobs with no open file on the mount that
// are idle, or the least recently used ones over maxOpenJobs. A reader a
// handle still holds keeps working (it holds its own reference); the next
// open just builds a fresh one.
func (e *Engine) trimOpen(now time.Time) int {
	active := e.streamJobs()
	e.mu.Lock()
	defer e.mu.Unlock()
	type cand struct {
		id   string
		used time.Time
	}
	var keep []cand
	dropped := 0
	for id, oj := range e.open {
		if active[id] {
			continue
		}
		if now.Sub(oj.used) >= openJobIdle {
			delete(e.open, id)
			dropped++
			continue
		}
		keep = append(keep, cand{id, oj.used})
	}
	if extra := len(e.open) - maxOpenJobs; extra > 0 && len(keep) > 0 {
		sort.Slice(keep, func(a, b int) bool { return keep[a].used.Before(keep[b].used) })
		for _, c := range keep[:min(extra, len(keep))] {
			delete(e.open, c.id)
			dropped++
		}
	}
	return dropped
}

// OpenJobs is how many jobs have built readers (for stats and tests).
func (e *Engine) OpenJobs() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.open)
}

// TopFolders lists the root of the mount.
func (e *Engine) TopFolders() []string {
	e.treeMu.RLock()
	defer e.treeMu.RUnlock()
	if e.tops == nil {
		return append([]string(nil), fixedTops...)
	}
	return append([]string(nil), e.tops...)
}

// Folders lists the job folders under a top folder.
func (e *Engine) Folders(top string) []*Folder {
	e.treeMu.RLock()
	defer e.treeMu.RUnlock()
	m := e.tree[top]
	out := make([]*Folder, 0, len(m))
	for _, f := range m {
		out = append(out, f)
	}
	sort.Slice(out, func(a, b int) bool { return out[a].Name < out[b].Name })
	return out
}

// Folder finds one job folder.
func (e *Engine) Folder(top, name string) (*Folder, bool) {
	e.treeMu.RLock()
	defer e.treeMu.RUnlock()
	f, ok := e.tree[top][name]
	return f, ok
}

// FolderPath is where a job's files appear in the mount.
func (e *Engine) FolderPath(id string) string {
	e.treeMu.RLock()
	defer e.treeMu.RUnlock()
	for name, f := range e.tree[AllFolder] {
		if f.JobID == id {
			return e.cfg.MountPath + "/" + AllFolder + "/" + name
		}
	}
	return ""
}

// rebuildTree recomputes the tree from ready jobs. A job keeps the folder
// name it was first given (store.Job.Folder); a new job whose name is
// taken gets " (2)", " (3)"... Names stay put when other jobs come and go,
// so a symlink never ends up on another job's files.
func (e *Engine) rebuildTree() { e.rebuildTreeWith("") }

// publish stores a job's result, puts its folder in the tree, and only then
// marks it ready: whoever sees "ready" (cli_debrid) also finds its folder,
// under its final name ("Name (2)" when "Name" is taken).
func (e *Engine) publish(id string, res *job.Result) (store.Job, error) {
	if err := e.Store.StoreResult(id, res); err != nil {
		return store.Job{}, err
	}
	e.rebuildTreeWith(id)
	j, err := e.Store.MarkReady(id, res)
	if err != nil {
		return store.Job{}, err
	}
	e.rebuildTree() // virtual folders may match on readiness-dependent fields
	return j, nil
}

// rebuildTreeWith rebuilds the tree treating job "becoming" as ready.
func (e *Engine) rebuildTreeWith(becoming string) {
	e.rebuildMu.Lock()
	defer e.rebuildMu.Unlock()
	jobs := e.Store.List()
	sort.Slice(jobs, func(a, b int) bool {
		if !jobs[a].CreatedAt.Equal(jobs[b].CreatedAt) {
			return jobs[a].CreatedAt.Before(jobs[b].CreatedAt)
		}
		return jobs[a].ID < jobs[b].ID
	})
	all := map[string]*Folder{}
	tree := map[string]map[string]*Folder{AllFolder: all}
	for _, t := range fixedTops[1:] {
		tree[t] = map[string]*Folder{}
	}
	for _, vf := range e.vfolders {
		tree[vf.name] = map[string]*Folder{} // listed even when empty
	}
	// Names held by jobs that have one, shown right now or not (a job
	// hidden for a while gets its own name back).
	held := map[string]string{}
	for _, j := range jobs {
		if keepsFolder(j) {
			if _, taken := held[j.Folder]; !taken {
				held[j.Folder] = j.ID
			}
		}
	}
	type placed struct {
		j       store.Job
		entries []job.Entry
	}
	var show []placed
	for _, j := range jobs {
		if j.Status != store.Ready && j.ID != becoming {
			continue
		}
		_, res, err := e.Store.Get(j.ID)
		if err != nil || res == nil {
			continue
		}
		entries := uniqueEntries(res.Entries)
		if len(j.Hidden) > 0 {
			visible := entries[:0:0]
			for _, en := range entries {
				if !contains(j.Hidden, en.Name) {
					visible = append(visible, en)
				}
			}
			entries = visible
		}
		if len(e.cfg.AllowedExt) > 0 {
			shown := entries[:0:0]
			for _, en := range entries {
				if extAllowed(e.cfg.AllowedExt, en.Name) {
					shown = append(shown, en)
				}
			}
			entries = shown
		}
		if len(entries) == 0 {
			continue
		}
		entries = markExtras(entries)
		show = append(show, placed{j, entries})
	}
	names := make(map[string]string, len(show)) // job id -> folder name
	for _, p := range show {
		if keepsFolder(p.j) && held[p.j.Folder] == p.j.ID {
			names[p.j.ID] = p.j.Folder
		}
	}
	var assigned []store.Job
	for _, p := range show {
		if _, ok := names[p.j.ID]; ok {
			continue
		}
		name := p.j.Name
		for n := 2; held[name] != ""; n++ {
			name = fmt.Sprintf("%s (%d)", p.j.Name, n)
		}
		held[name] = p.j.ID
		names[p.j.ID] = name
		p.j.Folder, p.j.FolderBase = name, p.j.Name
		assigned = append(assigned, p.j)
	}
	var providers []string
	for _, sh := range show {
		j, entries, name := sh.j, sh.entries, names[sh.j.ID]
		f := &Folder{JobID: j.ID, Name: name, Entries: entries, ModTime: j.CreatedAt}
		all[name] = f
		if j.Protocol == ProtocolDebrid {
			tree[TorrentFolder][name] = f
			if p := providerFolder(j); p != "" {
				if tree[p] == nil {
					tree[p] = map[string]*Folder{}
					providers = append(providers, p)
				}
				tree[p][name] = f
			}
		} else {
			tree[NZBFolder][name] = f
		}
		if j.Health != nil && j.Health.Status == health.Broken {
			tree[BadFolder][name] = f
		}
		for _, vf := range e.vfolders {
			if matches(vf.filters, j, f) {
				tree[vf.name][name] = f
			}
		}
	}
	for _, j := range assigned {
		folder, base := j.Folder, j.FolderBase
		if _, err := e.Store.Update(j.ID, func(s *store.Job) { s.Folder, s.FolderBase = folder, base }); err != nil &&
			!errors.Is(err, store.ErrNotFound) {
			e.log.Warn("folder name not saved", "job", j.ID, "folder", folder, "err", err)
		}
	}
	sort.Strings(providers)
	e.treeMu.Lock()
	e.tree = tree
	tops := append(append([]string(nil), fixedTops...), providers...)
	for _, vf := range e.vfolders {
		taken := false
		for _, t := range tops {
			taken = taken || t == vf.name
		}
		if !taken {
			tops = append(tops, vf.name)
		}
	}
	e.tops = tops
	e.treeMu.Unlock()
}

// keepsFolder: the job has a saved folder name still made from its name.
func keepsFolder(j store.Job) bool {
	return j.Folder != "" && j.FolderBase == j.Name
}

// uniqueEntries makes file names unique within a folder ("x.mkv",
// "x (2).mkv").
func uniqueEntries(in []job.Entry) []job.Entry {
	seen := map[string]bool{}
	out := make([]job.Entry, len(in))
	for i, en := range in {
		name := cleanFolderName(en.Name)
		ext := ""
		if dot := strings.LastIndex(name, "."); dot > 0 {
			ext = name[dot:]
		}
		base := strings.TrimSuffix(name, ext)
		for n := 2; seen[name]; n++ {
			name = fmt.Sprintf("%s (%d)%s", base, n, ext)
		}
		seen[name] = true
		en.Name = name
		out[i] = en
	}
	return out
}

// cleanFolderName makes a string safe as one path component.
var reNamePassword = regexp.MustCompile(`\{\{(.*?)\}\}`)

// splitNamePassword takes a "{{password}}" out of a job name.
func splitNamePassword(name string) (string, string) {
	m := reNamePassword.FindStringSubmatchIndex(name)
	if m == nil {
		return name, ""
	}
	pw := name[m[2]:m[3]]
	rest := strings.TrimSpace(name[:m[0]] + name[m[1]:])
	rest = strings.TrimSuffix(rest, ".nzb")
	return strings.TrimSpace(rest), pw
}

// withPasswordMeta adds <meta type="password"> to an NZB's head (creating
// the head if needed). ok is false when the result doesn't parse back to
// the same password; the NZB is then left alone.
func withPasswordMeta(data []byte, password string) ([]byte, bool) {
	var esc bytes.Buffer
	if err := xml.EscapeText(&esc, []byte(password)); err != nil {
		return data, false
	}
	meta := `<meta type="password">` + esc.String() + `</meta>`
	low := bytes.ToLower(data)
	var out []byte
	if i := bytes.Index(low, []byte("<head>")); i >= 0 {
		at := i + len("<head>")
		out = append(append(append([]byte(nil), data[:at]...), "\n  "+meta...), data[at:]...)
	} else if i := bytes.Index(low, []byte("<nzb")); i >= 0 {
		j := bytes.IndexByte(data[i:], '>')
		if j < 0 {
			return data, false
		}
		at := i + j + 1
		out = append(append(append([]byte(nil), data[:at]...), "\n<head>"+meta+"</head>"...), data[at:]...)
	} else {
		return data, false
	}
	parsed, err := nzb.Parse(bytes.NewReader(out))
	if err != nil || job.NZBPassword(parsed) != password {
		return data, false
	}
	return out, true
}

func cleanFolderName(s string) string {
	s = strings.Map(func(r rune) rune {
		if r == '/' || r == '\\' || r == 0 {
			return '_'
		}
		return r
	}, strings.TrimSpace(s))
	if s == "." || s == ".." {
		return "_"
	}
	return s
}

// Retry re-queues a failed job.
func (e *Engine) Retry(id string) (store.Job, error) {
	j, _, err := e.Store.Get(id)
	if err != nil {
		return store.Job{}, err
	}
	if j.Status != store.Failed {
		return j, fmt.Errorf("engine: job %s is %s, not failed", id, j.Status)
	}
	j, err = e.Store.Update(id, func(j *store.Job) { j.Status, j.Error, j.Retryable, j.Attempts = store.Queued, "", false, 0 })
	if err == nil {
		e.queue <- id
	}
	return j, err
}

// ---- health (feeds cli_debrid's repair system) ---------------------------

// ErrNotReady means the job has no import result to check yet.
var ErrNotReady = errors.New("engine: job is not ready")

// staleAfter: a sweep re-checks jobs whose last check is older than this.
const staleAfter = 7 * 24 * time.Hour

// CheckHealth starts a background check of one job. Checks share one
// worker slot by default and use prefetch priority, so playback wins.
func (e *Engine) CheckHealth(id string, full bool) error {
	j, res, err := e.Store.Get(id)
	if err != nil {
		return err
	}
	if j.Status != store.Ready || res == nil {
		return ErrNotReady
	}
	if j.Health != nil && j.Health.Status == "checking" {
		return nil // already running
	}
	prev := j.Health
	if _, err := e.Store.Update(id, func(j *store.Job) {
		h := store.Health{Status: "checking"}
		if prev != nil {
			h.Broken = prev.Broken // keep proof from playback until re-checked
		}
		j.Health = &h
	}); err != nil {
		return err
	}
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		if j.Protocol == ProtocolDebrid {
			e.runDebridCheck(id)
		} else {
			e.runCheck(id, full)
		}
	}()
	return nil
}

func (e *Engine) runCheck(id string, full bool) {
	select {
	case e.healthSem <- struct{}{}:
		defer func() { <-e.healthSem }()
	case <-e.ctx.Done():
		return
	}
	j, res, err := e.Store.Full(id)
	if err != nil && j.ID != "" && j.Status == store.Ready {
		// maps.bin unreadable: like cli_mount's corrupt .meta, report it
		// broken so repair replaces the release.
		e.log.Warn("segment maps unreadable; marking job broken", "job", id, "err", err)
		_, _ = e.Store.MarkUnreadable(id, err)
		return
	}
	if err != nil || res == nil {
		return
	}
	data, err := e.Store.NZB(id)
	if err != nil {
		return
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return
	}
	rep, err := health.Check(e.ctx, e.Client, parsed, res, healthOptions(e.cfg.HealthSample, full))
	if err != nil {
		if e.ctx.Err() == nil {
			e.log.Warn("health check failed", "job", id, "err", err)
		}
		return
	}
	// cli_mount's sweep also reads each media file's first bytes: a wrong
	// container header proves the file can't play even with every article
	// there (archive volumes stitched in the wrong order).
	badHeader := e.headerCheck(id, res, rep)
	probeUnknown := 0
	if e.cfg.MediaProbe {
		var probed []store.BrokenFile
		probed, probeUnknown = e.mediaCheck(id, res, rep, badHeader)
		badHeader = append(badHeader, probed...)
	}
	now := time.Now()
	e.Store.Update(id, func(j *store.Job) {
		h := store.Health{Status: rep.Status, CheckedAt: now, Inconclusive: rep.Inconclusive + probeUnknown}
		for _, f := range rep.Broken() {
			h.Broken = append(h.Broken, store.BrokenFile{Name: f.Name, Reason: ReasonSegmentMissing,
				Detail: f.Reason, Segment: f.Segment, Via: "check", At: now})
		}
		for _, b := range badHeader {
			if !hasBroken(h.Broken, b.Name) {
				h.Broken = append(h.Broken, b)
			}
			h.Status = health.Broken
		}
		// Proof from playback stays unless this check covered the same
		// article conclusively (it would then be listed above again).
		if j.Health != nil {
			for _, b := range j.Health.Broken {
				if readProof(b) && !hasBroken(h.Broken, b.Name) {
					h.Broken = append(h.Broken, b)
					h.Status = health.Broken
				}
			}
		}
		j.Health = &h
	})
	e.rebuildTree() // __bad__ follows health
	status := rep.Status
	if len(badHeader) > 0 {
		status = health.Broken
	}
	e.log.Info("health checked", "job", id, "status", status, "checked", rep.Checked, "inconclusive", rep.Inconclusive)
}

func hasBroken(list []store.BrokenFile, name string) bool {
	for _, b := range list {
		if b.Name == name {
			return true
		}
	}
	return false
}

// ReasonSegmentMissing is the cli_mount reason code for a file with an
// article missing on every provider; cli_debrid's playback repair acts on it.
const ReasonSegmentMissing = "usenet_segment_missing"

// More cli_mount reason codes cli_debrid's repair acts on.
const (
	ReasonHeaderInvalid   = "media_header_invalid"    // usenet file starts with the wrong container header
	ReasonManifestInvalid = "usenet_manifest_invalid" // the job's saved file map can't be read
	ReasonHosterGone      = "hoster_unavailable"      // debrid: the service says the file is gone
)

// MarkBroken records a file proven unplayable during playback (an article
// every provider answered missing for). detail is free text for logs/UI.
func (e *Engine) MarkBroken(id, file, reason, detail, segment string) {
	e.markBroken(id, file, reason, detail, segment, "playback")
}

// readProof: a broken mark from a real read (playback or prewarm) rather
// than a health check; a later check that didn't cover it keeps it.
func readProof(b store.BrokenFile) bool { return b.Via == "playback" || b.Via == "prewarm" }

// markBroken is MarkBroken with where the proof came from ("playback",
// "prewarm").
func (e *Engine) markBroken(id, file, reason, detail, segment, via string) {
	_, err := e.Store.Update(id, func(j *store.Job) {
		h := store.Health{Status: health.Broken}
		if j.Health != nil {
			h = *j.Health
			h.Status = health.Broken
		}
		if hasBroken(h.Broken, file) {
			return
		}
		h.Broken = append(h.Broken, store.BrokenFile{Name: file, Reason: reason, Detail: detail,
			Segment: segment, Via: via, At: time.Now()})
		j.Health = &h
	})
	if err == nil {
		e.rebuildTree()
		e.log.Warn("file marked broken", "via", via, "job", id, "file", file, "reason", reason, "detail", detail)
	}
}

// ClearHealth drops a job's health record (after a repair replaced it).
func (e *Engine) ClearHealth(id string) error {
	_, err := e.Store.Update(id, func(j *store.Job) { j.Health = nil })
	if err == nil {
		e.rebuildTree()
	}
	return err
}

func protocolMatches(scope, jobProtocol string) bool {
	switch strings.ToLower(scope) {
	case "nzb", "usenet":
		return jobProtocol != ProtocolDebrid
	case "torrent", "debrid":
		return jobProtocol == ProtocolDebrid
	}
	return true
}

// HealthRun is the state of a sweep over all ready jobs.
type HealthRun struct {
	ID        string    `json:"run_id"`
	Status    string    `json:"status"` // running, completed, stopped
	Full      bool      `json:"full"`
	Total     int       `json:"total"`
	Done      int       `json:"done"`
	StartedAt time.Time `json:"started_at"`
	EndedAt   time.Time `json:"ended_at,omitempty"`
}

// StartHealthRun checks every ready job (only those not checked in a week
// unless ignoreLastChecked). One run at a time; returns the current run.
// protocol limits the run like cli_mount's protocol scope: "nzb" (usenet),
// "torrent" (debrid) or "" for both.
func (e *Engine) StartHealthRun(full, ignoreLastChecked bool, protocol string) HealthRun {
	e.runMu.Lock()
	if e.run != nil && e.run.Status == "running" {
		r := *e.run
		e.runMu.Unlock()
		return r
	}
	var ids []string
	for _, j := range e.Store.List() {
		if j.Status != store.Ready || !protocolMatches(protocol, j.Protocol) {
			continue
		}
		if !ignoreLastChecked && j.Health != nil && time.Since(j.Health.CheckedAt) < staleAfter {
			continue
		}
		ids = append(ids, j.ID)
	}
	run := &HealthRun{ID: NewID(), Status: "running", Full: full, Total: len(ids), StartedAt: time.Now()}
	ctx, cancel := context.WithCancel(e.ctx)
	e.run, e.runCancel = run, cancel
	r := *run
	e.runMu.Unlock()

	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		for _, id := range ids {
			if ctx.Err() != nil {
				break
			}
			if err := e.CheckHealth(id, full); err == nil {
				e.waitChecked(ctx, id)
			}
			e.runMu.Lock()
			run.Done++
			e.runMu.Unlock()
		}
		e.runMu.Lock()
		run.EndedAt = time.Now()
		if ctx.Err() != nil {
			run.Status = "stopped"
		} else {
			run.Status = "completed"
		}
		e.runMu.Unlock()
		cancel()
	}()
	return r
}

// waitChecked blocks until a job's check left "checking".
func (e *Engine) waitChecked(ctx context.Context, id string) {
	for ctx.Err() == nil {
		j, _, err := e.Store.Get(id)
		if err != nil || j.Health == nil || j.Health.Status != "checking" {
			return
		}
		select {
		case <-ctx.Done():
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// HealthRunStatus returns the latest run (nil if none).
func (e *Engine) HealthRunStatus() *HealthRun {
	e.runMu.Lock()
	defer e.runMu.Unlock()
	if e.run == nil {
		return nil
	}
	r := *e.run
	return &r
}

// StopHealthRun stops the running sweep.
func (e *Engine) StopHealthRun() bool {
	e.runMu.Lock()
	defer e.runMu.Unlock()
	if e.run == nil || e.run.Status != "running" {
		return false
	}
	e.runCancel()
	return true
}
