package engine

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/store"
)

// Sidecars are small files written INTO the otherwise read-only mount:
// subtitles saved next to a video by cli_debrid's subtitle downloader,
// Bazarr, or a media server. They live on disk under
// <data_dir>/sidecars/<job id>/<name>, show up in the job's folder next to
// its streamed files, and go away with the job.

// MaxSidecarBytes bounds one sidecar file.
const MaxSidecarBytes = 16 << 20

// ErrNotSidecar: only subtitle files can be written into the mount.
var ErrNotSidecar = errors.New("engine: only subtitle files can be written into the mount")

var sidecarExts = map[string]bool{".srt": true, ".ass": true, ".ssa": true, ".sub": true, ".vtt": true, ".idx": true, ".sup": true}

// IsSidecarName reports whether name may be written into the mount.
func IsSidecarName(name string) bool {
	if name == "" || name != filepath.Base(name) || strings.ContainsAny(name, `/\`) || strings.HasPrefix(name, ".") {
		return false
	}
	return sidecarExts[strings.ToLower(filepath.Ext(name))]
}

// Sidecar is one stored file.
type Sidecar struct {
	Name    string    `json:"name"`
	Size    int64     `json:"size"`
	ModTime time.Time `json:"mod_time"`
}

func (e *Engine) sidecarDir(jobID string) string {
	return filepath.Join(e.cfg.DataDir, "sidecars", jobID)
}

// SidecarPath is the on-disk file of a sidecar (it may not exist).
func (e *Engine) SidecarPath(jobID, name string) (string, error) {
	if !store.ValidID(jobID) || !IsSidecarName(name) {
		return "", ErrNotSidecar
	}
	return filepath.Join(e.sidecarDir(jobID), name), nil
}

// Sidecars lists a job's sidecars, sorted by name.
func (e *Engine) Sidecars(jobID string) []Sidecar {
	if !store.ValidID(jobID) {
		return nil
	}
	ents, err := os.ReadDir(e.sidecarDir(jobID))
	if err != nil {
		return nil
	}
	var out []Sidecar
	for _, de := range ents {
		if de.IsDir() || !IsSidecarName(de.Name()) {
			continue
		}
		if fi, err := de.Info(); err == nil {
			out = append(out, Sidecar{Name: de.Name(), Size: fi.Size(), ModTime: fi.ModTime()})
		}
	}
	sort.Slice(out, func(a, b int) bool { return out[a].Name < out[b].Name })
	return out
}

// Sidecar finds one sidecar by name (case-insensitive when fold is set).
func (e *Engine) Sidecar(jobID, name string, fold bool) (Sidecar, bool) {
	for _, s := range e.Sidecars(jobID) {
		if s.Name == name || (fold && strings.EqualFold(s.Name, name)) {
			return s, true
		}
	}
	return Sidecar{}, false
}

// PutSidecar stores (or replaces) a sidecar atomically: readers never see
// a half-written file. A name that clashes with a streamed file is refused.
func (e *Engine) PutSidecar(jobID, name string, data []byte) error {
	p, err := e.SidecarPath(jobID, name)
	if err != nil {
		return err
	}
	if len(data) > MaxSidecarBytes {
		return fmt.Errorf("engine: sidecar %s is larger than %d bytes", name, MaxSidecarBytes)
	}
	j, res, err := e.Store.Get(jobID)
	if err != nil {
		return err
	}
	if j.Status != store.Ready || res == nil {
		return fmt.Errorf("engine: job %s is %s", jobID, j.Status)
	}
	for _, en := range res.Entries {
		if strings.EqualFold(en.Name, name) {
			return fmt.Errorf("engine: %s is a streamed file of this job", name)
		}
	}
	dir := filepath.Dir(p)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".sidecar-*")
	if err != nil {
		return err
	}
	_, werr := tmp.Write(data)
	cerr := tmp.Close()
	if werr == nil {
		werr = cerr
	}
	if werr == nil {
		werr = os.Rename(tmp.Name(), p)
	}
	if werr != nil {
		os.Remove(tmp.Name())
		return werr
	}
	e.log.Info("sidecar saved", "job", jobID, "file", name, "bytes", len(data))
	return nil
}

// DeleteSidecar removes one sidecar.
func (e *Engine) DeleteSidecar(jobID, name string) error {
	p, err := e.SidecarPath(jobID, name)
	if err != nil {
		return err
	}
	return os.Remove(p)
}

func (e *Engine) deleteSidecars(jobID string) {
	if store.ValidID(jobID) {
		os.RemoveAll(e.sidecarDir(jobID))
	}
}
