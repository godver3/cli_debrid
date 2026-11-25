package store

import (
	"bytes"
	"encoding/gob"
	"os"
	"path/filepath"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
)

// index.gob is a startup cache of every job's metadata and result summary
// (no segment maps), so Open reads one file instead of two per job. The
// per-job files stay the source of truth: a job is taken from the index
// only while its meta.json and result.json still have the size and mtime
// recorded with it; anything else (written after the last save, a crash,
// a missing or unreadable index) is read from its folder. The index is
// saved every indexEvery while something changed, and on Close.

const (
	indexFile    = "index.gob"
	indexVersion = 1
	indexEvery   = 30 * time.Second
)

// stamp identifies one version of a file; the zero stamp is "no file".
type stamp struct {
	Size  int64
	MTime int64 // ns
}

func statStamp(path string) stamp {
	fi, err := os.Stat(path)
	if err != nil {
		return stamp{}
	}
	return stamp{Size: fi.Size(), MTime: fi.ModTime().UnixNano()}
}

// stamps of a job's files as last written or read.
type stamps struct{ Meta, Result stamp }

type indexRec struct {
	Job    Job
	Result *job.Result
	Stamps stamps
}

type indexDoc struct {
	Version int
	Jobs    []indexRec
}

func (s *Store) loadIndex() map[string]indexRec {
	b, err := os.ReadFile(filepath.Join(s.dir, indexFile))
	if err != nil {
		return nil
	}
	var doc indexDoc
	if err := gob.NewDecoder(bytes.NewReader(b)).Decode(&doc); err != nil || doc.Version != indexVersion {
		return nil
	}
	out := make(map[string]indexRec, len(doc.Jobs))
	for _, r := range doc.Jobs {
		out[r.Job.ID] = r
	}
	return out
}

// saveIndex writes the index if anything changed since the last save.
func (s *Store) saveIndex() error {
	s.saveMu.Lock()
	defer s.saveMu.Unlock()
	if !s.dirty.Swap(false) {
		return nil
	}
	s.mu.RLock()
	doc := indexDoc{Version: indexVersion, Jobs: make([]indexRec, 0, len(s.jobs))}
	for id, j := range s.jobs {
		doc.Jobs = append(doc.Jobs, indexRec{Job: *j, Result: s.results[id], Stamps: s.stamps[id]})
	}
	s.mu.RUnlock()
	var buf bytes.Buffer
	if err := gob.NewEncoder(&buf).Encode(&doc); err != nil {
		s.dirty.Store(true)
		return err
	}
	if err := writeFile(filepath.Join(s.dir, indexFile), buf.Bytes()); err != nil {
		s.dirty.Store(true)
		return err
	}
	return nil
}

func (s *Store) saveLoop() {
	defer close(s.done)
	t := time.NewTicker(indexEvery)
	defer t.Stop()
	for {
		select {
		case <-s.stop:
			return
		case <-t.C:
			_ = s.saveIndex()
		}
	}
}

// Close stops the background index saves and saves it one last time.
func (s *Store) Close() error {
	s.closeOnce.Do(func() {
		close(s.stop)
		<-s.done
	})
	return s.saveIndex()
}
