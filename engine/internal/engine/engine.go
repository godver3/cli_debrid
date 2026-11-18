// Package engine ties the parts together: job store, NNTP client, caches,
// the import queue, and the folder tree the mount and API expose.
package engine

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/diskcache"
	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
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
	HealthSample  float64         `json:"health_sample_percent"`  // % checked by health checks (0 = 2%, max 30 per file)
	IdleTimeout   int             `json:"idle_timeout_seconds"`   // close NNTP connections unused this long (default 300)
	CacheExpiry   int             `json:"cache_expiry_seconds"`   // drop disk cache entries unused this long (0 = only by size)
	LinkCallback  string          `json:"link_callback"`          // cli_debrid URL that hands out debrid download links
	PreCacheMB    int             `json:"precache_mb"`            // on open, fetch this much of the start and end (0 = off)
	CacheCleanup  int             `json:"cache_cleanup_seconds"`  // how often the expiry runs (default 60)
	ImportWorkers int             `json:"import_workers"`         // jobs imported at once
	HealthWorkers int             `json:"health_workers"`         // jobs health-checked at once
	Parallel      int             `json:"parallel"`               // probes per import
	Providers     []nntp.Provider `json:"providers"`
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
	// RetryBackoff: waits before re-importing after an unproven failure
	// (default 1m, 5m, 15m); once used up the job fails, still retryable.
	RetryBackoff []time.Duration `json:"-"`
}

func (c *Config) defaults() {
	if c.MemCacheBytes <= 0 {
		c.MemCacheBytes = 256 << 20
	}
	if c.ReadAhead <= 0 {
		c.ReadAhead = 8
	}
	if c.ImportWorkers <= 0 {
		c.ImportWorkers = 2
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

func (c Config) readAhead() stream.ReadAhead {
	return stream.ReadAhead{Segments: c.ReadAhead, Bytes: int64(c.ReadAheadMB) << 20, MaxConns: c.FileConns}
}

// healthOptions turns a sample percentage into health.Options; a
// percentage the user set is not capped per file.
func healthOptions(percent float64, full bool) health.Options {
	if full || percent >= 100 {
		return health.Options{Full: true}
	}
	if percent <= 0 {
		return health.Options{} // built-in default: 2%, at most 30 per file
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
			if e.Disk != nil && expiry > 0 && time.Since(lastExpire) >= time.Duration(e.cfg.CacheCleanup)*time.Second {
				if n := e.Disk.Expire(expiry); n > 0 {
					e.log.Debug("disk cache expired", "segments", n)
				}
				lastExpire = time.Now()
			}
		}
	}
}

// Engine is safe for concurrent use.
type Engine struct {
	cfg    Config
	log    *slog.Logger
	Client *nntp.Client
	Cache  *stream.Cache
	Disk   *diskcache.Cache
	Store  *store.Store

	queue  chan string
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	mu   sync.Mutex
	open map[string]*job.Files // job id -> readers (built lazily)

	treeMu sync.RWMutex
	// rebuildMu serializes rebuildTree, so a slower rebuild can't install
	// an older tree over a newer one.
	rebuildMu sync.Mutex
	protect   protection // Plex playback protection
	started   time.Time
	vfolders  []namedFolder                 // compiled VirtualFolders, by name
	tree      map[string]map[string]*Folder // top folder -> folder name -> job folder
	tops      []string                      // top folders in listing order

	healthSem chan struct{}
	streams   streamTable
	links     *callbackLinks
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
	st, err := store.Open(cfg.DataDir)
	if err != nil {
		return nil, fmt.Errorf("engine: store: %w", err)
	}
	client, err := nntp.NewClient(cfg.Providers)
	if err != nil {
		return nil, fmt.Errorf("engine: %w", err)
	}
	e := &Engine{cfg: cfg, log: log, Client: client, Store: st,
		Cache: stream.NewCache(cfg.MemCacheBytes),
		queue: make(chan string, 4096), open: map[string]*job.Files{},
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
	e.Client.SetLogger(log.With("component", "nntp"))
	e.wg.Add(1)
	go e.maintain()
	e.Client.Warm(e.ctx)
	for _, j := range st.List() {
		if j.Status == store.Queued {
			e.queue <- j.ID
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

// Submit stores an NZB and queues its import. id may be given (migration
// keeps cli_mount's job ids); otherwise one is generated.
func (e *Engine) Submit(data []byte, name, category, id string) (store.Job, error) {
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return store.Job{}, fmt.Errorf("%w: %v", ErrBadNZB, err)
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

func (e *Engine) importJob(id string) {
	data, err := e.Store.NZB(id)
	if err != nil {
		return // deleted while queued
	}
	if _, err := e.Store.Update(id, func(j *store.Job) { j.Status = store.Importing }); err != nil {
		return
	}
	start := time.Now()
	fail := func(err error) {
		// Only failures that prove nothing may be retried (dead-marking
		// needs proof). Those are retried here, the job staying queued, so
		// cli_debrid never blacklists a release over a network blip.
		retry := errors.Is(err, job.ErrInconclusive)
		var attempts int
		j, _ := e.Store.Update(id, func(j *store.Job) {
			j.Error, j.Retryable = err.Error(), retry
			if retry && j.Attempts < len(e.cfg.RetryBackoff) {
				j.Status = store.Queued
				j.Attempts++
			} else {
				j.Status = store.Failed
			}
			attempts = j.Attempts
		})
		if j.Status == store.Queued {
			wait := e.cfg.RetryBackoff[attempts-1]
			e.log.Warn("import inconclusive, retrying", "job", id, "err", err, "attempt", attempts, "in", wait)
			time.AfterFunc(wait, func() { e.requeue(id) })
			return
		}
		e.log.Warn("import failed", "job", id, "name", j.Name, "err", err, "retryable", retry)
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		fail(fmt.Errorf("%w: %v", ErrBadNZB, err))
		return
	}
	ctx := e.ctx
	if e.cfg.ImportTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(e.ctx, time.Duration(e.cfg.ImportTimeout)*time.Second)
		defer cancel()
	}
	res, err := job.Build(ctx, e.Client, e.Cache, parsed, job.Options{Parallel: e.cfg.Parallel})
	if err == nil && e.cfg.DeobfuscateMode != "" {
		if j, _, gerr := e.Store.Get(id); gerr == nil {
			if n := Deobfuscate(res, e.cfg.DeobfuscateMode, j.Name); n > 0 {
				e.log.Debug("import: renamed obfuscated files", "job", id, "files", n, "mode", e.cfg.DeobfuscateMode)
			}
		}
	}
	if err == nil && e.cfg.ImportSample > 0 {
		err = e.importSample(ctx, parsed, res)
	}
	if err == nil && ctx.Err() != nil {
		// The deadline hit part-way: files not yet probed failed with a
		// context error, so a "success" here would be a partial job.
		err = ctx.Err()
	}
	if err != nil {
		if e.ctx.Err() != nil {
			return // shutting down: stays "importing", reopens as queued
		}
		if ctx.Err() != nil {
			err = fmt.Errorf("%w: import took longer than %ds", job.ErrInconclusive, e.cfg.ImportTimeout)
		}
		if res != nil && len(res.Failures) > 0 && !strings.Contains(err.Error(), res.Failures[0].Name) {
			err = fmt.Errorf("%w (%s)", err, failureSummary(res.Failures))
		}
		e.logBuild(id, res, true)
		fail(err)
		return
	}
	if _, err := e.Store.SetResult(id, res); err != nil {
		fail(err)
		return
	}
	e.rebuildTree()
	e.log.Info("imported", "job", id, "entries", len(res.Entries), "failures", len(res.Failures), "took", time.Since(start))
	e.logBuild(id, res, false)
}

// requeue puts a still-queued job back on the import queue (unless it was
// deleted or retried by hand meanwhile).
func (e *Engine) requeue(id string) {
	if e.ctx.Err() != nil {
		return
	}
	if j, _, err := e.Store.Get(id); err == nil && j.Status == store.Queued {
		select {
		case e.queue <- id:
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
func (e *Engine) Files(id string) (*job.Files, error) {
	e.mu.Lock()
	if fs, ok := e.open[id]; ok {
		e.mu.Unlock()
		return fs, nil
	}
	e.mu.Unlock()
	j, res, err := e.Store.Get(id)
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
	e.mu.Lock()
	defer e.mu.Unlock()
	if prev, ok := e.open[id]; ok {
		return prev, nil
	}
	e.open[id] = fs
	return fs, nil
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

// rebuildTree recomputes folder names from ready jobs. Names are unique:
// a later job whose name is taken gets " (2)", " (3)"... in creation order.
func (e *Engine) rebuildTree() {
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
	var providers []string
	for _, j := range jobs {
		if j.Status != store.Ready {
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
		name := j.Name
		for n := 2; all[name] != nil; n++ {
			name = fmt.Sprintf("%s (%d)", j.Name, n)
		}
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
	if j.Protocol == ProtocolDebrid {
		return ErrNotUsenet // article checks don't apply; a dead debrid link shows on read
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
		e.runCheck(id, full)
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
	_, res, err := e.Store.Get(id)
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
	now := time.Now()
	e.Store.Update(id, func(j *store.Job) {
		h := store.Health{Status: rep.Status, CheckedAt: now, Inconclusive: rep.Inconclusive}
		for _, f := range rep.Broken() {
			h.Broken = append(h.Broken, store.BrokenFile{Name: f.Name, Reason: ReasonSegmentMissing,
				Detail: f.Reason, Segment: f.Segment, Via: "check", At: now})
		}
		// Proof from playback stays unless this check covered the same
		// article conclusively (it would then be listed above again).
		if j.Health != nil {
			for _, b := range j.Health.Broken {
				if b.Via == "playback" && !hasBroken(h.Broken, b.Name) {
					h.Broken = append(h.Broken, b)
					h.Status = health.Broken
				}
			}
		}
		j.Health = &h
	})
	e.rebuildTree() // __bad__ follows health
	e.log.Info("health checked", "job", id, "status", rep.Status, "checked", rep.Checked, "inconclusive", rep.Inconclusive)
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

// MarkBroken records a file proven unplayable during playback (an article
// every provider answered missing for). detail is free text for logs/UI.
func (e *Engine) MarkBroken(id, file, detail, segment string) {
	_, err := e.Store.Update(id, func(j *store.Job) {
		h := store.Health{Status: health.Broken}
		if j.Health != nil {
			h = *j.Health
			h.Status = health.Broken
		}
		if hasBroken(h.Broken, file) {
			return
		}
		h.Broken = append(h.Broken, store.BrokenFile{Name: file, Reason: ReasonSegmentMissing, Detail: detail,
			Segment: segment, Via: "playback", At: time.Now()})
		j.Health = &h
	})
	if err == nil {
		e.rebuildTree()
		e.log.Warn("file marked broken during playback", "job", id, "file", file, "detail", detail)
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
func (e *Engine) StartHealthRun(full, ignoreLastChecked bool) HealthRun {
	e.runMu.Lock()
	if e.run != nil && e.run.Status == "running" {
		r := *e.run
		e.runMu.Unlock()
		return r
	}
	var ids []string
	for _, j := range e.Store.List() {
		if j.Status != store.Ready || j.Protocol == ProtocolDebrid {
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
