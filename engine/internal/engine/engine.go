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
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Top-level folders, matching cli_mount so existing symlinks keep resolving
// after a migration: every job under __all__, usenet jobs also under nzbs.
const (
	AllFolder = "__all__"
	NZBFolder = "nzbs"
)

// Config is the engine's configuration.
type Config struct {
	DataDir       string          `json:"data_dir"`
	CacheDir      string          `json:"cache_dir"`
	CacheBytes    int64           `json:"cache_bytes"`
	MemCacheBytes int64           `json:"mem_cache_bytes"`
	MountPath     string          `json:"mount_path"`
	Listen        string          `json:"listen"`
	Token         string          `json:"token"`
	ReadAhead     int             `json:"read_ahead"`     // segments prefetched after a live read
	ImportWorkers int             `json:"import_workers"` // jobs imported at once
	Parallel      int             `json:"parallel"`       // probes per import
	Providers     []nntp.Provider `json:"providers"`
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
	if c.Parallel <= 0 {
		c.Parallel = 8
	}
	if c.Listen == "" {
		c.Listen = "127.0.0.1:8283"
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
	tree   map[string]map[string]*Folder // top folder -> folder name -> job folder
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
		queue: make(chan string, 4096), open: map[string]*job.Files{}}
	if cfg.CacheDir != "" && cfg.CacheBytes > 0 {
		if e.Disk, err = diskcache.Open(cfg.CacheDir, cfg.CacheBytes); err != nil {
			return nil, fmt.Errorf("engine: disk cache: %w", err)
		}
		e.Cache.WithDisk(e.Disk)
	}
	e.ctx, e.cancel = context.WithCancel(context.Background())
	for i := 0; i < cfg.ImportWorkers; i++ {
		e.wg.Add(1)
		go e.worker()
	}
	for _, j := range st.List() {
		if j.Status == store.Queued {
			e.queue <- j.ID
		}
	}
	e.rebuildTree()
	return e, nil
}

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
		// Only failures that prove nothing may be retried (dead-marking needs proof).
		retry := errors.Is(err, job.ErrInconclusive)
		e.Store.Update(id, func(j *store.Job) { j.Status, j.Error, j.Retryable = store.Failed, err.Error(), retry })
		e.log.Warn("import failed", "job", id, "err", err, "retryable", retry)
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		fail(fmt.Errorf("%w: %v", ErrBadNZB, err))
		return
	}
	res, err := job.Build(e.ctx, e.Client, e.Cache, parsed, job.Options{Parallel: e.cfg.Parallel})
	if err != nil {
		if e.ctx.Err() != nil {
			return // shutting down: stays "importing", reopens as queued
		}
		fail(err)
		return
	}
	if _, err := e.Store.SetResult(id, res); err != nil {
		fail(err)
		return
	}
	e.rebuildTree()
	e.log.Info("imported", "job", id, "entries", len(res.Entries), "failures", len(res.Failures), "took", time.Since(start))
}

// Delete removes a job (and its folder) for good.
func (e *Engine) Delete(id string) error {
	if err := e.Store.Delete(id); err != nil {
		return err
	}
	e.mu.Lock()
	delete(e.open, id)
	e.mu.Unlock()
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
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	fs, err := job.Open(res, parsed, e.Client, e.Cache, e.cfg.ReadAhead)
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
func (e *Engine) TopFolders() []string { return []string{AllFolder, NZBFolder} }

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
	jobs := e.Store.List()
	sort.Slice(jobs, func(a, b int) bool {
		if !jobs[a].CreatedAt.Equal(jobs[b].CreatedAt) {
			return jobs[a].CreatedAt.Before(jobs[b].CreatedAt)
		}
		return jobs[a].ID < jobs[b].ID
	})
	all := map[string]*Folder{}
	for _, j := range jobs {
		if j.Status != store.Ready {
			continue
		}
		_, res, err := e.Store.Get(j.ID)
		if err != nil || res == nil {
			continue
		}
		name := j.Name
		for n := 2; all[name] != nil; n++ {
			name = fmt.Sprintf("%s (%d)", j.Name, n)
		}
		all[name] = &Folder{JobID: j.ID, Name: name, Entries: uniqueEntries(res.Entries), ModTime: j.CreatedAt}
	}
	e.treeMu.Lock()
	e.tree = map[string]map[string]*Folder{AllFolder: all, NZBFolder: all}
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
	j, err = e.Store.Update(id, func(j *store.Job) { j.Status, j.Error, j.Retryable = store.Queued, "", false })
	if err == nil {
		e.queue <- id
	}
	return j, err
}
