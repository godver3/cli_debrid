package engine

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// cli_mount's mounted-media probe (pkg/manager/media_probe.go): ffprobe
// must find an audio or video stream. Its limits and verdicts are copied:
// 4 at once, 60 s each, probesize 5M; ffprobe failing outright is
// media_probe_failed, no audio/video stream is media_no_playable_stream,
// timeouts and a missing ffprobe prove nothing.
//
// ffprobe reads the file over HTTP from a loopback server in this process,
// never through the engine's own mount: a process that forks while files
// on its own FUSE mount are open can deadlock (cli_mount's FrostyNight
// hang). A read error while ffprobe reads is never blamed on the file
// unless it is a proven missing article.
const (
	mediaProbeConcurrency = 4
	mediaProbeTimeout     = 60 * time.Second
)

// Media probe outcomes, as cli_mount's.
const (
	ReasonProbeFailed     = "media_probe_failed"
	ReasonNoPlayable      = "media_no_playable_stream"
	reasonProbeTimeout    = "media_probe_timeout"
	reasonProbeUnavailble = "media_probe_unavailable"
)

type probeResult struct {
	state   string // health.Healthy / health.Broken / "unknown"
	reason  string
	segment string // when a missing article was the cause
}

type probeRec struct {
	jobID string
	entry job.Entry
	mu    sync.Mutex
	err   error // first read error while ffprobe read
}

type mediaProber struct {
	e       *Engine
	once    sync.Once
	initErr error
	base    string // http://127.0.0.1:port/probe/<token>/
	slots   chan struct{}
	mu      sync.Mutex
	recs    map[string]*probeRec
	srv     *http.Server
}

func (p *mediaProber) start() error {
	p.once.Do(func() {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			p.initErr = err
			return
		}
		tok := make([]byte, 16)
		_, _ = rand.Read(tok)
		prefix := "/probe/" + hex.EncodeToString(tok) + "/"
		p.base = "http://" + ln.Addr().String() + prefix
		p.slots = make(chan struct{}, mediaProbeConcurrency)
		p.recs = map[string]*probeRec{}
		mux := http.NewServeMux()
		mux.HandleFunc(prefix, func(w http.ResponseWriter, r *http.Request) {
			id := strings.TrimPrefix(r.URL.Path, prefix)
			p.mu.Lock()
			rec := p.recs[id]
			p.mu.Unlock()
			if rec == nil {
				http.NotFound(w, r)
				return
			}
			files, err := p.e.Files(rec.jobID)
			if err != nil {
				rec.note(err)
				http.Error(w, "job not readable", http.StatusServiceUnavailable)
				return
			}
			ra := &recordingReader{ctx: r.Context(), r: files.Entry(rec.entry), rec: rec}
			http.ServeContent(w, r, rec.entry.Name, time.Time{}, io.NewSectionReader(ra, 0, rec.entry.Size))
		})
		p.srv = &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
		go p.srv.Serve(ln)
	})
	return p.initErr
}

func (p *mediaProber) close() {
	if p.srv != nil {
		p.srv.Close()
	}
}

func (rec *probeRec) note(err error) {
	if err == nil || errors.Is(err, io.EOF) || errors.Is(err, context.Canceled) {
		return
	}
	rec.mu.Lock()
	if rec.err == nil || (nntp.IsNotFound(err) && !nntp.IsNotFound(rec.err)) {
		rec.err = err // a proven miss outranks other trouble
	}
	rec.mu.Unlock()
}

type recordingReader struct {
	ctx context.Context
	r   *job.EntryReader
	rec *probeRec
}

func (rr *recordingReader) ReadAt(b []byte, off int64) (int, error) {
	n, err := rr.r.ReadAtCtx(rr.ctx, nntp.Read, b, off)
	if err != nil && !(errors.Is(err, io.EOF) && off+int64(n) >= rr.r.Size()) {
		rr.rec.note(err)
	}
	return n, err
}

// probe runs ffprobe on one entry.
func (p *mediaProber) probe(ctx context.Context, jobID string, en job.Entry) probeResult {
	bin := p.e.cfg.FFprobe
	if bin == "" {
		bin = "ffprobe"
	}
	path, err := exec.LookPath(bin)
	if err != nil {
		return probeResult{state: "unknown", reason: reasonProbeUnavailble}
	}
	if err := p.start(); err != nil {
		return probeResult{state: "unknown", reason: reasonProbeUnavailble}
	}
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	case <-ctx.Done():
		return probeResult{state: "unknown", reason: reasonProbeTimeout}
	}
	ctx, cancel := context.WithTimeout(ctx, mediaProbeTimeout)
	defer cancel()
	id := NewID()
	rec := &probeRec{jobID: jobID, entry: en}
	p.mu.Lock()
	p.recs[id] = rec
	p.mu.Unlock()
	defer func() {
		p.mu.Lock()
		delete(p.recs, id)
		p.mu.Unlock()
	}()
	cmd := exec.CommandContext(ctx, path, "-v", "error", "-probesize", "5M", "-analyzeduration", "5000000",
		"-show_entries", "stream=codec_type", "-of", "json", p.base+id)
	out, err := cmd.Output()
	// A read failure while ffprobe read decides first: proof only when an
	// article is missing everywhere, otherwise nothing is known.
	rec.mu.Lock()
	readErr := rec.err
	rec.mu.Unlock()
	if readErr != nil {
		if nntp.IsNotFound(readErr) {
			seg := ""
			var se *stream.SegmentError
			if errors.As(readErr, &se) {
				seg = se.ID
			}
			return probeResult{state: "broken", reason: ReasonSegmentMissing, segment: seg}
		}
		return probeResult{state: "unknown", reason: reasonProbeUnavailble}
	}
	return classifyProbe(ctx, out, err)
}

// classifyProbe is cli_mount's runFFProbe/classifyFFProbeOutput.
func classifyProbe(ctx context.Context, out []byte, err error) probeResult {
	if ctx.Err() != nil {
		return probeResult{state: "unknown", reason: reasonProbeTimeout}
	}
	if err != nil {
		var execErr *exec.Error
		if errors.As(err, &execErr) || errors.Is(err, fs.ErrNotExist) || errors.Is(err, fs.ErrPermission) {
			return probeResult{state: "unknown", reason: reasonProbeUnavailble}
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			stderr := strings.ToLower(string(exitErr.Stderr))
			switch {
			case strings.Contains(stderr, "timed out"), strings.Contains(stderr, "timeout"):
				return probeResult{state: "unknown", reason: reasonProbeTimeout}
			case strings.Contains(stderr, "permission denied"),
				strings.Contains(stderr, "resource temporarily unavailable"),
				strings.Contains(stderr, "connection reset"),
				strings.Contains(stderr, "connection refused"),
				strings.Contains(stderr, "server returned 5"), // our loopback server: a read failed
				strings.Contains(stderr, "input/output error"):
				return probeResult{state: "unknown", reason: reasonProbeUnavailble}
			}
		}
		return probeResult{state: "broken", reason: ReasonProbeFailed}
	}
	var doc struct {
		Streams []struct {
			CodecType string `json:"codec_type"`
		} `json:"streams"`
	}
	if json.Unmarshal(out, &doc) != nil {
		return probeResult{state: "unknown", reason: reasonProbeUnavailble}
	}
	for _, s := range doc.Streams {
		if strings.EqualFold(s.CodecType, "audio") || strings.EqualFold(s.CodecType, "video") {
			return probeResult{state: "healthy"}
		}
	}
	return probeResult{state: "broken", reason: ReasonNoPlayable}
}
