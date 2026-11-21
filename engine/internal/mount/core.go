package mount

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

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

	// Read-ahead for this handle (stream/hint.go): ctx ends its prefetches
	// on close; next/seq track how far it has read sequentially.
	ctx    context.Context
	cancel context.CancelFunc
	mu     sync.Mutex
	next   int64 // offset after the last read, -1 before the first
	seq    int64 // bytes read sequentially since the last jump
	jumps  int   // jumps since the handle last read scanSeq in order
}

const (
	// minAhead: read-ahead for a handle that has not read sequentially yet
	// (a probe, or playback right after a seek).
	minAhead = 4 << 20
	// seqSlack: reads this close to the last one count as sequential
	// (kernel readahead reorders and overlaps them).
	seqSlack = 4 << 20
	// scanJumps: a handle that has jumped this many times without reading
	// scanSeq sequentially is a scanner (Plex thumbnails, intro/credit
	// detection), not playback: playback jumps a few times (head, tail,
	// resume point) and then reads in order. A scanner gets no minimum
	// read-ahead, only what its own sequential reading earns: found live,
	// one thumbnail pass reading a few hundred KB per jump made the full
	// read-ahead pull ~10 MB/s for 1 MB/s of reads.
	scanJumps = 4
	scanSeq   = 16 << 20
	// readTimeout bounds one mount read (cli_mount's ReadTimeout): a read
	// stuck on a stalled server ends with an error instead of holding a
	// FUSE worker until the player gives up.
	readTimeout = 120 * time.Second
)

// hint returns the read-ahead hint for a read of n bytes at off: twice
// what this handle has read sequentially so far, at least minAhead unless
// the handle reads like a scanner and nobody reports the file playing.
func (h *openFile) hint(off int64, n int) stream.ReadHint {
	h.mu.Lock()
	if h.next >= 0 && off >= h.next-seqSlack && off <= h.next+seqSlack {
		h.seq += int64(n)
		h.next = max(h.next, off+int64(n))
		if h.seq >= scanSeq {
			h.jumps = 0 // reading in order now: playback after its seeks
		}
	} else { // first read, or a jump: start counting again here
		if h.next >= 0 {
			h.jumps++
		}
		h.seq, h.next = int64(n), off+int64(n)
	}
	floor := int64(minAhead)
	if h.jumps >= scanJumps {
		if h.e == nil {
			floor = 0
		} else if playing, _ := h.e.Playing(h.jobID, h.name); !playing {
			floor = 0
		}
	}
	ahead := max(floor, 2*h.seq, 1)
	h.mu.Unlock()
	return stream.ReadHint{Ahead: ahead, Owner: h.ctx}
}

func openEntry(e *engine.Engine, log *slog.Logger, jobID string, en job.Entry) (*openFile, error) {
	files, err := e.Files(jobID)
	if err != nil {
		log.Warn("open failed", "job", jobID, "file", en.Name, "err", err)
		return nil, err
	}
	h := &openFile{r: files.Entry(en), name: en.Name, jobID: jobID, e: e, log: log, next: -1}
	h.ctx, h.cancel = context.WithCancel(context.Background())
	h.stream = e.OpenStream(jobID, en.Name, en.Size)
	go e.PreCache(jobID, en)
	return h, nil
}

// readAt fills dest from off; n < len(dest) with a nil error is EOF.
func (h *openFile) readAt(ctx context.Context, dest []byte, off int64) (int, error) {
	if off >= h.r.Size() {
		return 0, nil
	}
	ctx, cancel := context.WithTimeout(ctx, readTimeout)
	defer cancel()
	ctx = stream.WithReadHint(ctx, h.hint(off, len(dest)))
	n, err := h.r.ReadAtCtx(ctx, h.e.ReadPriority(h.jobID, h.name), dest, off)
	if err != nil && n < len(dest) && off+int64(n) < h.r.Size() {
		h.log.Warn("read failed", "job", h.jobID, "file", h.name, "size", h.r.Size(), "offset", off, "len", len(dest), "err", err,
			"missing", nntp.IsNotFound(err))
		var dead *stream.DeadFileError
		switch {
		case nntp.IsNotFound(err):
			// Proof the file can't play: hand it to cli_debrid's repair.
			seg := ""
			var se *stream.SegmentError
			if errors.As(err, &se) {
				seg = se.ID
			}
			h.e.MarkBroken(h.jobID, h.name, engine.ReasonSegmentMissing, "article missing on every provider during playback", seg)
		case errors.As(err, &dead):
			// The debrid service said the file is gone (the same answer
			// cli_mount's repair sweep counts as broken).
			h.e.MarkBroken(h.jobID, h.name, dead.Reason, dead.Detail, "")
		}
		return 0, ErrIO
	}
	h.e.StreamRead(h.stream, off, n)
	return n, nil
}

func (h *openFile) close() {
	h.cancel() // the handle's read-ahead is for nobody now
	h.e.CloseStream(h.stream)
}

// ---- path lookups (used by the path-based Windows mount) ------------------

// node kinds
const (
	kindNone = iota
	kindRoot
	kindTop
	kindFolder
	kindFile
	kindSidecar
)

type resolved struct {
	kind    int
	top     string
	folder  *engine.Folder
	entry   job.Entry
	sidecar engine.Sidecar
	name    string // last path element as given (for creating a sidecar)
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
	if sc, ok := e.Sidecar(f.JobID, parts[2], true); ok {
		return resolved{kind: kindSidecar, top: parts[0], folder: f, sidecar: sc, name: sc.Name}
	}
	// Not there: callers creating a sidecar need the folder and the name.
	return resolved{folder: f, top: parts[0], name: parts[2]}
}

func foldCase(e *engine.Engine, top, name string) *engine.Folder {
	for _, f := range e.Folders(top) {
		if strings.EqualFold(f.Name, name) {
			return f
		}
	}
	return nil
}

// ---- sidecar writes (subtitles saved into the mount) -----------------------

// sidecarWriter buffers one sidecar being written; commit stores it.
type sidecarWriter struct {
	mu    sync.Mutex
	e     *engine.Engine
	jobID string
	name  string
	buf   []byte
	dirty bool
}

// newSidecarWriter starts from the stored file unless trunc is set.
func newSidecarWriter(e *engine.Engine, jobID, name string, trunc bool) *sidecarWriter {
	w := &sidecarWriter{e: e, jobID: jobID, name: name, dirty: trunc}
	if !trunc {
		if p, err := e.SidecarPath(jobID, name); err == nil {
			w.buf, _ = os.ReadFile(p)
		}
	}
	return w
}

func (w *sidecarWriter) writeAt(data []byte, off int64) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	end := off + int64(len(data))
	if off < 0 || end > engine.MaxSidecarBytes {
		return 0, ErrIO
	}
	if end > int64(len(w.buf)) {
		w.buf = append(w.buf, make([]byte, end-int64(len(w.buf)))...)
	}
	copy(w.buf[off:], data)
	w.dirty = true
	return len(data), nil
}

func (w *sidecarWriter) readAt(dest []byte, off int64) int {
	w.mu.Lock()
	defer w.mu.Unlock()
	if off >= int64(len(w.buf)) {
		return 0
	}
	return copy(dest, w.buf[off:])
}

func (w *sidecarWriter) truncate(size int64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if size < 0 || size > engine.MaxSidecarBytes {
		return
	}
	if size <= int64(len(w.buf)) {
		w.buf = w.buf[:size]
	} else {
		w.buf = append(w.buf, make([]byte, size-int64(len(w.buf)))...)
	}
	w.dirty = true
}

func (w *sidecarWriter) size() int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return int64(len(w.buf))
}

// commit stores the file if it changed (called on flush and release).
func (w *sidecarWriter) commit() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.dirty {
		return nil
	}
	if err := w.e.PutSidecar(w.jobID, w.name, w.buf); err != nil {
		return err
	}
	w.dirty = false
	return nil
}

// readSidecar reads a stored sidecar from disk.
func readSidecar(e *engine.Engine, jobID, name string, dest []byte, off int64) (int, error) {
	p, err := e.SidecarPath(jobID, name)
	if err != nil {
		return 0, err
	}
	f, err := os.Open(p)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	n, err := f.ReadAt(dest, off)
	if errors.Is(err, io.EOF) {
		err = nil
	}
	return n, err
}
