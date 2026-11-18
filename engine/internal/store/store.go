// Package store keeps jobs on disk: one folder per job holding the
// original NZB, the job's metadata and its import result. Everything is
// loaded into memory at start and written atomically on change.
//
//	<dir>/jobs/<id>/meta.json
//	<dir>/jobs/<id>/job.nzb
//	<dir>/jobs/<id>/result.json
//	<dir>/deleted.json          tombstones for the changes feed
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
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
	Provider  string         `json:"provider,omitempty"`
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
	Via     string    `json:"via"` // "check" or "playback"
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
	dir     string
	mu      sync.RWMutex
	jobs    map[string]*Job
	results map[string]*job.Result
	deleted []Tombstone
	now     func() time.Time
}

// Open loads every job under dir (created if missing).
func Open(dir string) (*Store, error) {
	s := &Store{dir: dir, jobs: map[string]*Job{}, results: map[string]*job.Result{}, now: time.Now}
	if err := os.MkdirAll(filepath.Join(dir, "jobs"), 0o755); err != nil {
		return nil, err
	}
	ents, err := os.ReadDir(filepath.Join(dir, "jobs"))
	if err != nil {
		return nil, err
	}
	for _, e := range ents {
		if !e.IsDir() || !ValidID(e.Name()) {
			continue
		}
		var j Job
		if err := readJSON(s.path(e.Name(), "meta.json"), &j); err != nil {
			continue // half-written job from a crash; ignore it
		}
		if j.Status == Importing || j.Status == Queued {
			j.Status = Queued // interrupted import: the engine re-runs it
		}
		if j.Health != nil && j.Health.Status == "checking" {
			j.Health.Status = "unknown" // interrupted check; proof already found is kept
			if len(j.Health.Broken) > 0 {
				j.Health.Status = "broken"
			}
		}
		s.jobs[j.ID] = &j
		var r job.Result
		if err := readJSON(s.path(j.ID, "result.json"), &r); err == nil {
			s.results[j.ID] = &r
		}
	}
	_ = readJSON(filepath.Join(dir, "deleted.json"), &s.deleted)
	return s, nil
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
	return j, nil
}

// Update changes a job's metadata through fn and saves it.
func (s *Store) Update(id string, fn func(*Job)) (Job, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
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
	return next, nil
}

// SetResult stores an import result and marks the job ready.
func (s *Store) SetResult(id string, r *job.Result) (Job, error) {
	if err := writeJSON(s.path(id, "result.json"), r); err != nil {
		return Job{}, err
	}
	s.mu.Lock()
	s.results[id] = r
	s.mu.Unlock()
	var size int64
	for _, e := range r.Entries {
		size += e.Size
	}
	return s.Update(id, func(j *Job) { j.Status, j.Error, j.Size = Ready, "", size })
}

// Get returns a job and its result (nil until ready).
func (s *Store) Get(id string) (Job, *job.Result, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	j, ok := s.jobs[id]
	if !ok {
		return Job{}, nil, ErrNotFound
	}
	return *j, s.results[id], nil
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
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return ErrNotFound
	}
	if err := os.RemoveAll(filepath.Join(s.dir, "jobs", id)); err != nil {
		return err
	}
	delete(s.jobs, id)
	delete(s.results, id)
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
