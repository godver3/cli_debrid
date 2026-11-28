// Package store keeps jobs on disk: one folder per job holding the
// original NZB, the job's metadata and its import result. Metadata and
// result summaries are held in memory (loaded at start, mostly from the
// index); segment maps are read only when a job's files are opened or
// checked. Every file is written atomically.
//
//	<dir>/jobs/<id>/meta.json
//	<dir>/jobs/<id>/job.nzb
//	<dir>/jobs/<id>/result.json  result without segment maps
//	<dir>/jobs/<id>/maps.bin     segment maps of mapped (migrated) sources
//	<dir>/index.gob             startup cache of meta + result summaries
//	<dir>/deleted.json          tombstones for the changes feed
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
)

// Status of a job.
const (
	Queued    = "queued"
	Importing = "importing"
	Ready     = "ready"
	Failed    = "failed"
)

// Job is a stored job's metadata.
type Job struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Category string `json:"category"`
	Status   string `json:"status"`
	Error    string `json:"error,omitempty"`
	// Retryable marks failures that were not proof (network trouble):
	// resubmitting later may work. Leftover-only or unusable NZBs are final.
	Retryable bool `json:"retryable,omitempty"`
	Attempts  int  `json:"attempts,omitempty"` // imports retried after unproven failures
	// Protocol is "debrid" for debrid jobs ("" = usenet).
	Protocol string `json:"protocol,omitempty"`
	// Provider of a debrid job (rd, tb...): its folder next to torrents/.
	Provider string `json:"provider,omitempty"`
	// Hash is a debrid job's torrent info hash (cli_debrid's repair finds
	// library items by it).
	Hash      string         `json:"hash,omitempty"`
	Size      int64          `json:"size"` // sum of entry sizes once ready
	Tags      []string       `json:"tags,omitempty"`
	CLIIDs    map[string]int `json:"cli_ids,omitempty"` // entry name -> cli_debrid item id
	CreatedAt time.Time      `json:"created_at"`
	UpdatedAt time.Time      `json:"updated_at"`
	Health    *Health        `json:"health,omitempty"`
	// Hidden lists entry names removed by repair cleanup: the rest of the
	// job (other episodes of a pack) stays visible.
	Hidden []string `json:"hidden,omitempty"`
	// Folder is the job's folder name on the mount once it was given one
	// ("Name" or "Name (2)" when another job had the name first), and
	// FolderBase the Name it was made from. Kept so a folder never changes
	// name (symlinks point at it) until the job is renamed.
	Folder     string `json:"folder,omitempty"`
	FolderBase string `json:"folder_base,omitempty"`
}

// Health is the latest health record of a job, for cli_debrid's repair.
type Health struct {
	// Status: checking, healthy, broken or unknown (nothing conclusive).
	Status       string       `json:"status"`
	CheckedAt    time.Time    `json:"checked_at,omitempty"`
	Broken       []BrokenFile `json:"broken_files,omitempty"`
	Inconclusive int          `json:"inconclusive,omitempty"`
}

// BrokenFile is a file proven unplayable.
type BrokenFile struct {
	Name string `json:"file_name"`
	// Reason is a cli_mount-compatible code cli_debrid's repair acts on
	// (usenet_segment_missing); Detail says what exactly was found.
	Reason  string    `json:"reason"`
	Detail  string    `json:"detail,omitempty"`
	Segment string    `json:"segment,omitempty"`
	Via     string    `json:"via"` // "check", "playback" or "prewarm"
	At      time.Time `json:"at"`
}

// Tombstone records a deleted job for the changes feed.
type Tombstone struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	DeletedAt time.Time `json:"deleted_at"`
}

var (
	ErrNotFound = errors.New("store: job not found")
	ErrExists   = errors.New("store: job id already exists")
	ErrBadID    = errors.New("store: invalid job id")
)

var reID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// ValidID reports whether id is safe to use as a folder name.
func ValidID(id string) bool { return reID.MatchString(id) }

// tombstoneKeep bounds how long deletions stay in the changes feed.
const tombstoneKeep = 30 * 24 * time.Hour

// Store is safe for concurrent use.
type Store struct {
	dir string
	mu  sync.RWMutex
	// results hold summaries: mapped sources' segment maps stay in
	// maps.bin until Full loads them.
	jobs    map[string]*Job
	results map[string]*job.Result
	stamps  map[string]stamps
	deleted []Tombstone
	now     func() time.Time

	imports      map[string]*context.CancelFunc
	importCommit sync.Mutex // serializes import publication with deletion

	dirty     atomic.Bool // index out of date
	saveMu    sync.Mutex
	stop      chan struct{}
	done      chan struct{}
	closeOnce sync.Once
}

// Open loads every job under dir (created if missing): from the index for
// jobs whose files are unchanged since it was saved, from their folders
// otherwise. Results written before maps.bin existed (segment maps inline
// in result.json) are converted on the way.
func Open(dir string) (*Store, error) {
	s := &Store{dir: dir, jobs: map[string]*Job{}, results: map[string]*job.Result{},
		stamps: map[string]stamps{}, imports: map[string]*context.CancelFunc{}, now: time.Now, stop: make(chan struct{}), done: make(chan struct{})}
	if err := os.MkdirAll(filepath.Join(dir, "jobs"), 0o755); err != nil {
		return nil, err
	}
	ents, err := os.ReadDir(filepath.Join(dir, "jobs"))
	if err != nil {
		return nil, err
	}
	idx := s.loadIndex()
	var pending []string
	for _, e := range ents {
		if !e.IsDir() || !ValidID(e.Name()) {
			continue
		}
		id := e.Name()
		if rec, ok := idx[id]; ok && rec.Job.ID == id && rec.Stamps.Meta != (stamp{}) &&
			statStamp(s.path(id, "meta.json")) == rec.Stamps.Meta &&
			statStamp(s.path(id, "result.json")) == rec.Stamps.Result {
			j := rec.Job
			s.jobs[id] = &j
			if rec.Result != nil {
				s.results[id] = rec.Result
			}
			s.stamps[id] = rec.Stamps
			continue
		}
		pending = append(pending, id)
	}
	if len(pending) > 0 || len(idx) != len(s.jobs) {
		s.dirty.Store(true)
	}
	s.loadFolders(pending)
	for _, j := range s.jobs {
		if j.Status == Importing || j.Status == Queued {
			j.Status = Queued // interrupted import: the engine re-runs it
		}
		if j.Health != nil && j.Health.Status == "checking" {
			j.Health.Status = "unknown" // interrupted check; proof already found is kept
			if len(j.Health.Broken) > 0 {
				j.Health.Status = "broken"
			}
		}
	}
	_ = readJSON(filepath.Join(dir, "deleted.json"), &s.deleted)
	_ = s.saveIndex()
	go s.saveLoop()
	return s, nil
}

// loadFolders reads jobs from their folders, several at a time (the first
// start after the maps.bin change converts every mapped result).
func (s *Store) loadFolders(ids []string) {
	if len(ids) == 0 {
		return
	}
	work := make(chan string)
	var wg sync.WaitGroup
	for range min(len(ids), max(2, runtime.GOMAXPROCS(0))) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for id := range work {
				j, r, st, ok := s.loadFolder(id)
				if !ok {
					continue
				}
				s.mu.Lock()
				s.jobs[id] = j
				if r != nil {
					s.results[id] = r
				}
				s.stamps[id] = st
				s.mu.Unlock()
			}
		}()
	}
	for _, id := range ids {
		work <- id
	}
	close(work)
	wg.Wait()
}

func (s *Store) loadFolder(id string) (*Job, *job.Result, stamps, bool) {
	var j Job
	if err := readJSON(s.path(id, "meta.json"), &j); err != nil || j.ID != id {
		return nil, nil, stamps{}, false // half-written job from a crash; ignore it
	}
	st := stamps{Meta: statStamp(s.path(id, "meta.json"))}
	var r job.Result
	err := readJSON(s.path(id, "result.json"), &r)
	if err != nil {
		if j.Status == Ready && j.Protocol != "debrid" {
			lostResult(&j, err, s.now()) // debrid: the engine drops it and the mirror adds it back
		}
		st.Result = statStamp(s.path(id, "result.json"))
		return &j, nil, st, true
	}
	res := &r
	if !r.MapsFile {
		if sum, maps := splitMaps(&r); maps != nil {
			// Old layout: maps inline. Write maps.bin first, so a crash
			// leaves the old result.json (converted again next start).
			if err := writeFile(s.path(id, mapsFile), maps); err == nil {
				if err := writeJSON(s.path(id, "result.json"), sum); err == nil {
					res = sum
				}
			}
			// On a write error the full result stays in memory and works.
		}
	}
	st.Result = statStamp(s.path(id, "result.json"))
	return &j, res, st, true
}

// lostResult: a ready job whose file map (result.json) can't be read has
// nothing to stream. Like cli_mount's lost or corrupt segment map, that is
// reported as broken (written atomically, so never a write in progress) so
// cli_debrid's repair replaces it, instead of the job silently vanishing
// from the mount.
func lostResult(j *Job, err error, now time.Time) {
	reason := "usenet_manifest_invalid"
	var names []string
	for n := range j.CLIIDs {
		names = append(names, n)
	}
	sort.Strings(names)
	if len(names) == 0 {
		names = []string{j.Name}
	}
	h := Health{Status: "broken", CheckedAt: now}
	for _, n := range names {
		h.Broken = append(h.Broken, BrokenFile{Name: n, Reason: reason, Detail: "file map unreadable: " + err.Error(),
			Via: "check", At: now})
	}
	j.Health = &h
}

func (s *Store) path(id, name string) string { return filepath.Join(s.dir, "jobs", id, name) }

// Create stores a new job and its NZB. j.ID must be valid and unused.
func (s *Store) Create(j Job, nzbData []byte) (Job, error) {
	if !ValidID(j.ID) {
		return Job{}, ErrBadID
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.jobs[j.ID]; ok {
		return Job{}, ErrExists
	}
	now := s.now()
	j.CreatedAt, j.UpdatedAt = now, now
	if j.Status == "" {
		j.Status = Queued
	}
	if err := os.MkdirAll(filepath.Join(s.dir, "jobs", j.ID), 0o755); err != nil {
		return Job{}, err
	}
	if err := writeFile(s.path(j.ID, "job.nzb"), nzbData); err != nil {
		return Job{}, err
	}
	if err := writeJSON(s.path(j.ID, "meta.json"), &j); err != nil {
		return Job{}, err
	}
	s.jobs[j.ID] = &j
	s.stamps[j.ID] = stamps{Meta: statStamp(s.path(j.ID, "meta.json"))}
	s.dirty.Store(true)
	return j, nil
}

// Update changes a job's metadata through fn and saves it.
func (s *Store) Update(id string, fn func(*Job)) (Job, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.updateLocked(id, fn)
}

func (s *Store) updateLocked(id string, fn func(*Job)) (Job, error) {
	cur, ok := s.jobs[id]
	if !ok {
		return Job{}, ErrNotFound
	}
	next := *cur
	fn(&next)
	next.ID = cur.ID
	next.UpdatedAt = s.now()
	if err := writeJSON(s.path(id, "meta.json"), &next); err != nil {
		return Job{}, err
	}
	*cur = next
	st := s.stamps[id]
	st.Meta = statStamp(s.path(id, "meta.json"))
	s.stamps[id] = st
	s.dirty.Store(true)
	return next, nil
}

// BeginImport claims a queued job and ties the attempt to the job's lifetime.
func (s *Store) BeginImport(parent context.Context, id string) (Job, context.Context, func(), error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := parent.Err(); err != nil {
		return Job{}, nil, nil, err
	}
	j, ok := s.jobs[id]
	if !ok {
		return Job{}, nil, nil, ErrNotFound
	}
	if j.Status != Queued {
		return Job{}, nil, nil, fmt.Errorf("store: job %s cannot be imported (%s)", id, j.Status)
	}
	started, err := s.updateLocked(id, func(j *Job) { j.Status = Importing })
	if err != nil {
		return Job{}, nil, nil, err
	}
	ctx, cancel := context.WithCancel(parent)
	s.imports[id] = &cancel
	return started, ctx, func() {
		s.mu.Lock()
		if s.imports[id] == &cancel {
			delete(s.imports, id)
		}
		s.mu.Unlock()
		cancel()
	}, nil
}

// CommitImport applies an attempt's outcome if its job still exists. The
// callback can change metadata and publish a folder atomically with respect
// to deletion, including when the same ID is submitted again.
func (s *Store) CommitImport(ctx context.Context, commit func()) {
	s.importCommit.Lock()
	defer s.importCommit.Unlock()
	if ctx.Err() == nil {
		commit()
	}
}

// SetResult stores an import result and marks the job ready.
func (s *Store) SetResult(id string, r *job.Result) (Job, error) {
	if err := s.StoreResult(id, r); err != nil {
		return Job{}, err
	}
	return s.MarkReady(id, r)
}

// StoreResult saves an import result without changing the job's status
// (the engine places the folder first, then calls MarkReady). Segment maps
// go to maps.bin; the store keeps the rest in memory. r is not changed.
func (s *Store) StoreResult(id string, r *job.Result) error {
	sum, maps := splitMaps(r)
	if maps != nil {
		if err := writeFile(s.path(id, mapsFile), maps); err != nil {
			return err
		}
	}
	if err := writeJSON(s.path(id, "result.json"), sum); err != nil {
		return err
	}
	if maps == nil {
		_ = os.Remove(s.path(id, mapsFile)) // replaced a mapped result
	}
	s.mu.Lock()
	s.results[id] = sum
	st := s.stamps[id]
	st.Result = statStamp(s.path(id, "result.json"))
	s.stamps[id] = st
	s.mu.Unlock()
	s.dirty.Store(true)
	return nil
}

// MarkReady marks a job with a stored result ready.
func (s *Store) MarkReady(id string, r *job.Result) (Job, error) {
	var size int64
	for _, e := range r.Entries {
		size += e.Size
	}
	return s.Update(id, func(j *Job) { j.Status, j.Error, j.Size = Ready, "", size })
}

// Get returns a job and its result summary (nil until ready): mapped
// sources carry no segment map; Full loads them.
func (s *Store) Get(id string) (Job, *job.Result, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	j, ok := s.jobs[id]
	if !ok {
		return Job{}, nil, ErrNotFound
	}
	return *j, s.results[id], nil
}

// Full returns a job and its complete result, segment maps included (read
// from maps.bin): what streaming and article checks need.
func (s *Store) Full(id string) (Job, *job.Result, error) {
	j, r, err := s.Get(id)
	if err != nil || r == nil || !r.MapsFile {
		return j, r, err
	}
	full, err := withMaps(r, s.path(id, mapsFile))
	if err != nil {
		return j, nil, fmt.Errorf("store: job %s segment maps: %w", id, err)
	}
	return j, full, nil
}

// MarkUnreadable reports a ready job whose segment maps can't be read as
// broken (usenet_manifest_invalid), so cli_debrid's repair replaces it.
func (s *Store) MarkUnreadable(id string, cause error) (Job, error) {
	return s.Update(id, func(j *Job) { lostResult(j, cause, s.now()) })
}

// NZB returns a job's stored NZB.
func (s *Store) NZB(id string) ([]byte, error) {
	s.mu.RLock()
	_, ok := s.jobs[id]
	s.mu.RUnlock()
	if !ok {
		return nil, ErrNotFound
	}
	return os.ReadFile(s.path(id, "job.nzb"))
}

// List returns all jobs, newest first.
func (s *Store) List() []Job {
	s.mu.RLock()
	out := make([]Job, 0, len(s.jobs))
	for _, j := range s.jobs {
		out = append(out, *j)
	}
	s.mu.RUnlock()
	sort.Slice(out, func(a, b int) bool {
		if !out[a].CreatedAt.Equal(out[b].CreatedAt) {
			return out[a].CreatedAt.After(out[b].CreatedAt)
		}
		return out[a].ID < out[b].ID
	})
	return out
}

// Delete removes a job and records a tombstone.
func (s *Store) Delete(id string) error {
	s.importCommit.Lock()
	defer s.importCommit.Unlock()
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return ErrNotFound
	}
	if err := os.RemoveAll(filepath.Join(s.dir, "jobs", id)); err != nil {
		return err
	}
	if cancel := s.imports[id]; cancel != nil {
		(*cancel)()
		delete(s.imports, id)
	}
	delete(s.jobs, id)
	delete(s.results, id)
	delete(s.stamps, id)
	s.dirty.Store(true)
	now := s.now()
	keep := s.deleted[:0]
	for _, t := range s.deleted {
		if now.Sub(t.DeletedAt) < tombstoneKeep && t.ID != id {
			keep = append(keep, t)
		}
	}
	s.deleted = append(keep, Tombstone{ID: id, Name: j.Name, DeletedAt: now})
	return writeJSON(filepath.Join(s.dir, "deleted.json"), s.deleted)
}

// Changes returns jobs updated and jobs deleted after since.
func (s *Store) Changes(since time.Time) ([]Job, []Tombstone) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var jobs []Job
	for _, j := range s.jobs {
		if j.UpdatedAt.After(since) {
			jobs = append(jobs, *j)
		}
	}
	sort.Slice(jobs, func(a, b int) bool { return jobs[a].UpdatedAt.Before(jobs[b].UpdatedAt) })
	var gone []Tombstone
	for _, t := range s.deleted {
		if t.DeletedAt.After(since) {
			gone = append(gone, t)
		}
	}
	return jobs, gone
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", " ")
	if err != nil {
		return err
	}
	return writeFile(path, b)
}

// writeFile replaces path atomically (temp file + rename).
func writeFile(path string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		os.Remove(tmp.Name())
		return fmt.Errorf("store: %w", err)
	}
	return nil
}
