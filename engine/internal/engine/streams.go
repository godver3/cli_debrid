package engine

import (
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Stream is one open file on the mount, for "now playing" and stats.
type Stream struct {
	ID        uint64    `json:"id"`
	JobID     string    `json:"job_id"`
	Folder    string    `json:"folder"`
	File      string    `json:"file"`
	Protocol  string    `json:"protocol"` // "usenet" (debrid later)
	Size      int64     `json:"size"`
	OpenedAt  time.Time `json:"opened_at"`
	LastRead  time.Time `json:"last_read,omitempty"`
	BytesRead int64     `json:"bytes_read"`
	Position  int64     `json:"position"`   // end of the latest read
	RateBytes int64     `json:"rate_bytes"` // bytes/s over the last few seconds
}

type streamState struct {
	mu sync.Mutex
	s  Stream
	// rate window
	winStart time.Time
	winBytes int64
}

type streamTable struct {
	next atomic.Uint64
	mu   sync.Mutex
	m    map[uint64]*streamState
}

// OpenStream registers an open file; the returned id goes to StreamRead
// and CloseStream.
func (e *Engine) OpenStream(jobID, file string, size int64) uint64 {
	t := &e.streams
	id := t.next.Add(1)
	folder := ""
	if j, _, err := e.Store.Get(jobID); err == nil {
		folder = j.Name
	}
	now := time.Now()
	st := &streamState{s: Stream{ID: id, JobID: jobID, Folder: folder, File: file, Protocol: "usenet",
		Size: size, OpenedAt: now}, winStart: now}
	t.mu.Lock()
	if t.m == nil {
		t.m = map[uint64]*streamState{}
	}
	t.m[id] = st
	t.mu.Unlock()
	return id
}

// StreamRead records n bytes read at off.
func (e *Engine) StreamRead(id uint64, off int64, n int) {
	t := &e.streams
	t.mu.Lock()
	st := t.m[id]
	t.mu.Unlock()
	if st == nil || n <= 0 {
		return
	}
	now := time.Now()
	st.mu.Lock()
	st.s.LastRead = now
	st.s.BytesRead += int64(n)
	st.s.Position = off + int64(n)
	st.winBytes += int64(n)
	if el := now.Sub(st.winStart); el >= 3*time.Second {
		st.s.RateBytes = int64(float64(st.winBytes) / el.Seconds())
		st.winStart, st.winBytes = now, 0
	}
	st.mu.Unlock()
}

// CloseStream forgets a closed file.
func (e *Engine) CloseStream(id uint64) {
	t := &e.streams
	t.mu.Lock()
	delete(t.m, id)
	t.mu.Unlock()
}

// Streams lists open files, most recently read first. A file not read for
// a while reports no rate (paused or merely open).
func (e *Engine) Streams() []Stream {
	t := &e.streams
	t.mu.Lock()
	all := make([]*streamState, 0, len(t.m))
	for _, st := range t.m {
		all = append(all, st)
	}
	t.mu.Unlock()
	out := make([]Stream, 0, len(all))
	for _, st := range all {
		st.mu.Lock()
		s := st.s
		st.mu.Unlock()
		if time.Since(s.LastRead) > 10*time.Second {
			s.RateBytes = 0
		}
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].LastRead.After(out[j].LastRead) })
	return out
}

// RequestShutdown asks the process to exit cleanly (unmounting first). The
// API's POST /shutdown uses it: on Windows the engine runs detached, with no
// console to send a signal to.
func (e *Engine) RequestShutdown() {
	e.stopOnce.Do(func() { close(e.stopReq) })
}

// ShutdownRequested is closed by RequestShutdown.
func (e *Engine) ShutdownRequested() <-chan struct{} { return e.stopReq }
