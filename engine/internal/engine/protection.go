package engine

import (
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Plex playback protection (cli_mount's feature). cli_debrid polls Plex's
// /status/sessions and posts the mount files Plex says are playing. While
// at least one is AND an import is queued or running (cli_mount's
// background-contention rule: with no imports there is nothing for playback
// to be protected from), only reads of those files are Live and may use the
// playback reserve; every other mount read (Plex scanner, thumbnails,
// intro detection, ffprobe) is Read: ahead of prefetch, outside the
// reserve. Without a fresh, successful poll everything stays Live, so a
// Plex outage never slows playback down.

// ProtectionTTL: a protected set older than this is ignored (the poller
// stopped or cli_debrid is down).
const ProtectionTTL = 45 * time.Second

// ProtectedFile is one file Plex reports playing.
type ProtectedFile struct {
	JobID string `json:"job_id"`
	File  string `json:"file"`
}

type protection struct {
	mu    sync.RWMutex
	files map[ProtectedFile]bool
	ok    bool // the last poll succeeded
	at    time.Time
	err   string
}

// SetProtected records the result of one Plex poll. ok=false (Plex
// unreachable) turns protection off until the next good poll.
func (e *Engine) SetProtected(files []ProtectedFile, ok bool, pollErr string) {
	m := make(map[ProtectedFile]bool, len(files))
	for _, f := range files {
		if f.JobID != "" && f.File != "" {
			m[f] = true
		}
	}
	e.protect.mu.Lock()
	e.protect.files, e.protect.ok, e.protect.at, e.protect.err = m, ok, time.Now(), pollErr
	e.protect.mu.Unlock()
}

// active: protection applies right now (enabled, fresh good poll,
// something is playing, and imports are competing for connections).
func (e *Engine) protectionActive() bool {
	return e.cfg.PlexProtection && e.protect.ok && time.Since(e.protect.at) < ProtectionTTL &&
		len(e.protect.files) > 0 && e.importContention()
}

// importContention: an import is running or waiting in the queue.
func (e *Engine) importContention() bool {
	return e.importsActive.Load() > 0 || len(e.queue) > 0
}

// ReadPriority is the NNTP priority of a mount read of jobID/file.
func (e *Engine) ReadPriority(jobID, file string) nntp.Priority {
	if !e.cfg.PlexProtection {
		return nntp.Live
	}
	e.protect.mu.RLock()
	defer e.protect.mu.RUnlock()
	if !e.protectionActive() || e.protect.files[ProtectedFile{JobID: jobID, File: file}] {
		return nntp.Live
	}
	return nntp.Read
}

// Playing reports whether the media server says jobID/file is playing;
// known is false when nobody is asking it (protection off, or no fresh
// successful poll), so callers can't tell either way.
func (e *Engine) Playing(jobID, file string) (playing, known bool) {
	if !e.cfg.PlexProtection {
		return false, false
	}
	e.protect.mu.RLock()
	defer e.protect.mu.RUnlock()
	if !e.protect.ok || time.Since(e.protect.at) >= ProtectionTTL {
		return false, false
	}
	return e.protect.files[ProtectedFile{JobID: jobID, File: file}], true
}

// ProtectionStatus is for status pages.
type ProtectionStatus struct {
	Enabled bool            `json:"enabled"`
	Active  bool            `json:"active"`
	PollOK  bool            `json:"poll_ok"`
	PollAt  time.Time       `json:"poll_at,omitempty"`
	Error   string          `json:"error,omitempty"`
	Files   []ProtectedFile `json:"files"`
}

func (e *Engine) Protection() ProtectionStatus {
	e.protect.mu.RLock()
	defer e.protect.mu.RUnlock()
	st := ProtectionStatus{Enabled: e.cfg.PlexProtection, Active: e.protectionActive(), PollOK: e.protect.ok,
		PollAt: e.protect.at, Error: e.protect.err, Files: []ProtectedFile{}}
	for f := range e.protect.files {
		st.Files = append(st.Files, f)
	}
	return st
}
