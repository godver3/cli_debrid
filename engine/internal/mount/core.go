package mount

import (
	"context"
	"errors"
	"log/slog"
	"strings"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Platform-independent pieces shared by the Linux (go-fuse) and Windows
// (cgofuse/WinFsp) mounts.

// ErrIO is a read that can't be served (the adapters map it to EIO).
var ErrIO = errors.New("mount: read failed")

// openFile is one open file: reads, stream tracking for "now playing",
// and playback-miss reporting to cli_debrid's repair.
type openFile struct {
	r      *job.EntryReader
	name   string
	jobID  string
	e      *engine.Engine
	log    *slog.Logger
	stream uint64
}

func openEntry(e *engine.Engine, log *slog.Logger, jobID string, en job.Entry) (*openFile, error) {
	files, err := e.Files(jobID)
	if err != nil {
		log.Warn("open failed", "job", jobID, "file", en.Name, "err", err)
		return nil, err
	}
	h := &openFile{r: files.Entry(en), name: en.Name, jobID: jobID, e: e, log: log}
	h.stream = e.OpenStream(jobID, en.Name, en.Size)
	go e.PreCache(jobID, en)
	return h, nil
}

// readAt fills dest from off; n < len(dest) with a nil error is EOF.
func (h *openFile) readAt(ctx context.Context, dest []byte, off int64) (int, error) {
	if off >= h.r.Size() {
		return 0, nil
	}
	n, err := h.r.ReadAtCtx(ctx, nntp.Live, dest, off)
	if err != nil && n < len(dest) && off+int64(n) < h.r.Size() {
		h.log.Warn("read failed", "file", h.name, "offset", off, "len", len(dest), "err", err,
			"missing", nntp.IsNotFound(err))
		if nntp.IsNotFound(err) {
			// Proof the file can't play: hand it to cli_debrid's repair.
			seg := ""
			var se *stream.SegmentError
			if errors.As(err, &se) {
				seg = se.ID
			}
			h.e.MarkBroken(h.jobID, h.name, "article missing on every provider during playback", seg)
		}
		return 0, ErrIO
	}
	h.e.StreamRead(h.stream, off, n)
	return n, nil
}

func (h *openFile) close() { h.e.CloseStream(h.stream) }

// ---- path lookups (used by the path-based Windows mount) ------------------

// node kinds
const (
	kindNone = iota
	kindRoot
	kindTop
	kindFolder
	kindFile
)

type resolved struct {
	kind   int
	top    string
	folder *engine.Folder
	entry  job.Entry
}

// resolve maps "/__all__/<folder>/<file>" (either slash) to the tree.
func resolve(e *engine.Engine, p string) resolved {
	p = strings.Trim(strings.ReplaceAll(p, `\`, "/"), "/")
	if p == "" {
		return resolved{kind: kindRoot}
	}
	parts := strings.Split(p, "/")
	found := false
	for _, t := range e.TopFolders() {
		if strings.EqualFold(t, parts[0]) {
			parts[0], found = t, true
		}
	}
	if !found {
		return resolved{}
	}
	if len(parts) == 1 {
		return resolved{kind: kindTop, top: parts[0]}
	}
	f, ok := e.Folder(parts[0], parts[1])
	if !ok {
		f = foldCase(e, parts[0], parts[1]) // Windows paths are case-insensitive
		if f == nil {
			return resolved{}
		}
	}
	if len(parts) == 2 {
		return resolved{kind: kindFolder, top: parts[0], folder: f}
	}
	if len(parts) != 3 {
		return resolved{}
	}
	for _, en := range f.Entries {
		if en.Name == parts[2] || strings.EqualFold(en.Name, parts[2]) {
			return resolved{kind: kindFile, top: parts[0], folder: f, entry: en}
		}
	}
	return resolved{}
}

func foldCase(e *engine.Engine, top, name string) *engine.Folder {
	for _, f := range e.Folders(top) {
		if strings.EqualFold(f.Name, name) {
			return f
		}
	}
	return nil
}
