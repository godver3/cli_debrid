package stream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// RemoteChunk is the unit fetched from a debrid provider (one HTTP range).
// Fewer, larger ranges than usenet articles: debrid CDNs throttle and reset
// connections when one file gets many small parallel requests.
const RemoteChunk = 4 << 20

// RemoteMaxConns caps parallel range requests per debrid file.
const RemoteMaxConns = 4

// RemoteOptions tune debrid streaming (zero values = the defaults above).
// A LinkSource that also implements RemoteOptioner hands them to every
// file it serves.
type RemoteOptions struct {
	ChunkBytes     int64  // one HTTP range / cache unit
	MaxConns       int    // parallel range requests per file
	ReadAheadBytes int64  // prefetch after each live read (0 = the usenet read-ahead)
	UserAgent      string // sent with every download request ("" = Go's default)
	Client         *http.Client
}

// RemoteOptioner is a LinkSource with streaming options.
type RemoteOptioner interface {
	RemoteOptions() RemoteOptions
}

// DeadFileError is the debrid service's proof that a file is gone (the
// same answers cli_mount's repair counts as broken: hoster unavailable,
// file removed, no link). Only this marks a debrid file broken; timeouts
// and other errors never do.
type DeadFileError struct {
	Reason string // cli_mount reason code, e.g. hoster_unavailable
	Detail string
}

func (e *DeadFileError) Error() string {
	if e.Detail != "" {
		return "debrid file gone (" + e.Reason + "): " + e.Detail
	}
	return "debrid file gone (" + e.Reason + ")"
}

// LinkSource turns a file reference into a direct download URL. refresh
// asks for a new link (the cached one expired or was refused).
type LinkSource interface {
	Link(ctx context.Context, ref string, refresh bool) (string, error)
}

// ErrLinkRefused: the provider kept refusing the file's link.
var ErrLinkRefused = errors.New("stream: debrid link refused")

// RemoteFile reads a debrid file over HTTP ranges, in 1 MiB chunks that go
// through the same caches (memory, disk) as usenet articles.
type RemoteFile struct {
	ref       string
	size      int64
	chunkSize int64
	userAgent string
	links     LinkSource
	cache     *Cache
	client    *http.Client
	readAhead int
	slots     chan struct{}

	mu         sync.Mutex
	prefetched map[int]bool

	// live playback streams (remote_stream.go)
	smu     sync.Mutex
	scond   *sync.Cond
	streams []*downloader
	// failErr is the last stream's final error (guarded by smu): reads in
	// the next streamFailHold get it at once instead of each paying the
	// whole retry cycle again (~15 s on a file the service keeps refusing:
	// a player's 128 KB reads stacked into minutes).
	failErr error
	failAt  time.Time
}

// OpenRemote wraps a debrid file of the given size.
func OpenRemote(links LinkSource, client *http.Client, cache *Cache, ref string, size int64, ra ReadAhead) *RemoteFile {
	var o RemoteOptions
	if op, ok := links.(RemoteOptioner); ok {
		o = op.RemoteOptions()
	}
	if client == nil || client == http.DefaultClient {
		client = o.Client
	}
	if client == nil {
		client = defaultRemoteClient // with dial/header timeouts
	}
	chunk := int64(RemoteChunk)
	if o.ChunkBytes > 0 {
		chunk = o.ChunkBytes
	}
	if o.ReadAheadBytes > 0 {
		ra = ReadAhead{Bytes: o.ReadAheadBytes, MaxConns: ra.MaxConns}
	}
	r := &RemoteFile{ref: ref, size: size, links: links, cache: cache, client: client, chunkSize: chunk,
		userAgent: o.UserAgent, prefetched: map[int]bool{}}
	r.readAhead = ra.segments(chunk)
	conns := RemoteMaxConns
	if o.MaxConns > 0 {
		conns = o.MaxConns
	} else if ra.MaxConns > 0 {
		conns = min(conns, ra.MaxConns)
	}
	r.slots = make(chan struct{}, conns)
	r.scond = sync.NewCond(&r.smu)
	return r
}

// Size is the file size.
func (r *RemoteFile) Size() int64 { return r.size }

func (r *RemoteFile) chunkLen(i int) int64 {
	return min(r.chunkSize, r.size-int64(i)*r.chunkSize)
}

// key names chunk i; the chunk size is part of it so chunks cached with
// another size (an older version) are never mistaken for these.
func (r *RemoteFile) key(i int) string {
	return "remote:" + strconv.FormatInt(r.chunkSize, 10) + ":" + r.ref + ":" + strconv.Itoa(i)
}

func (r *RemoteFile) chunk(ctx context.Context, prio nntp.Priority, i int) ([]byte, error) {
	data, err := r.fetchChunk(ctx, prio, i)
	if err == nil && int64(len(data)) != r.chunkLen(i) {
		return nil, fmt.Errorf("stream: chunk %d of %s has %d bytes, want %d", i, r.ref, len(data), r.chunkLen(i))
	}
	return data, err
}

func (r *RemoteFile) fetchChunk(ctx context.Context, prio nntp.Priority, i int) ([]byte, error) {
	return r.cache.DoCtx(ctx, r.key(i), prio, func(ctx context.Context) ([]byte, error) {
		start, n := int64(i)*r.chunkSize, r.chunkLen(i)
		var last error
		lastStatus := 0
		relinked := false
		for attempt := 0; attempt < 5; attempt++ {
			url, err := r.links.Link(ctx, r.ref, false)
			if err != nil {
				countersFor(r.ref).linkFail(err)
				return nil, fmt.Errorf("stream: link for %s: %w", r.ref, err)
			}
			// On the wire now: a live read for this chunk joins this fetch
			// instead of downloading it a second time (found live: it halved
			// debrid throughput).
			nntp.FireAcquireHook(ctx)
			data, status, err := r.get(ctx, url, start, n)
			if err == nil {
				return data, nil
			}
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			last, lastStatus = err, status
			if badLinkResponse(status, err) {
				// The link expired or was refused: one fresh link, then give up.
				if relinked {
					return nil, last
				}
				relinked = true
				if _, err := r.links.Link(ctx, r.ref, true); err != nil {
					return nil, fmt.Errorf("stream: new link for %s: %w", r.ref, err)
				}
				continue
			}
			if serverUnreachable(status, err) && !relinked {
				// The link's server is down: one fresh link, which may be on
				// another server, then the usual retries.
				relinked = true
				if _, err := refreshUnreachable(ctx, r.links, r.ref, url); err != nil {
					return nil, fmt.Errorf("stream: new link for %s: %w", r.ref, err)
				}
				continue
			}
			// 429/5xx, connection reset, stall: transient, same link after a pause.
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(500<<attempt) * time.Millisecond):
			}
		}
		if lastStatus == http.StatusServiceUnavailable && !relinked {
			// Still 503: the file may be gone. A fresh link asks the service,
			// whose answer (dead file or not) is the real error.
			if _, err := r.links.Link(ctx, r.ref, true); err != nil {
				return nil, fmt.Errorf("stream: new link for %s: %w", r.ref, err)
			}
		}
		return nil, last
	})
}

func (r *RemoteFile) get(parent context.Context, url string, start, n int64) ([]byte, int, error) {
	ctx, wd := newWatchdog(parent)
	defer wd.stop()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, start+n-1))
	if r.userAgent != "" {
		req.Header.Set("User-Agent", r.userAgent)
	}
	st := countersFor(r.ref)
	st.begin()
	defer st.end()
	began := time.Now()
	resp, err := r.client.Do(req)
	if err != nil {
		if wd.stalled() {
			err = fmt.Errorf("stream: no answer for %s: %w", remoteStallTimeout(), err)
		}
		if parent.Err() == nil {
			st.fail(err)
		}
		return nil, 0, err
	}
	wd.progress()
	st.headers(began)
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusPartialContent:
		if err := checkContentRange(resp, start, start+n-1); err != nil {
			st.fail(err)
			return nil, resp.StatusCode, err
		}
	case resp.StatusCode == http.StatusOK && start == 0 && n == r.size:
	default:
		io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		err := fmt.Errorf("%w: HTTP %d for bytes %d-%d", ErrLinkRefused, resp.StatusCode, start, start+n-1)
		st.fail(err)
		return nil, resp.StatusCode, err
	}
	data, err := readExact(progressReader{r: resp.Body, w: wd}, n)
	st.bytes.Add(int64(len(data)))
	if err != nil {
		if wd.stalled() {
			err = fmt.Errorf("stream: no data for %s: %w", remoteStallTimeout(), err)
		}
		if parent.Err() == nil {
			st.fail(err)
		}
		return nil, resp.StatusCode, err
	}
	if int64(len(data)) != n {
		err := fmt.Errorf("stream: got %d bytes, want %d", len(data), n)
		st.fail(err)
		return nil, resp.StatusCode, err
	}
	return data, resp.StatusCode, nil
}

// ReadAtCtx reads len(p) bytes at off.
func (r *RemoteFile) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("stream: negative offset")
	}
	if off >= r.size {
		return 0, io.EOF
	}
	if prio != nntp.Prefetch {
		// Playback: sequential streams that start where the player reads
		// (remote_stream.go). Chunk fetches below are for pre-cache/prewarm.
		return r.readStream(ctx, p, off)
	}
	n, last := 0, -1
	for n < len(p) && off+int64(n) < r.size {
		i := int((off + int64(n)) / r.chunkSize)
		data, err := r.chunk(ctx, prio, i)
		if err != nil {
			return n, err
		}
		n += copy(p[n:], data[off+int64(n)-int64(i)*r.chunkSize:])
		last = i
	}
	if prio != nntp.Prefetch && r.readAhead > 0 && last >= 0 {
		r.startReadAhead(last + 1)
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func (r *RemoteFile) startReadAhead(from int) {
	total := int((r.size + r.chunkSize - 1) / r.chunkSize)
	for i := from; i < from+r.readAhead && i < total; i++ {
		if _, ok := r.cache.Get(r.key(i)); ok {
			continue
		}
		r.mu.Lock()
		started := r.prefetched[i]
		r.prefetched[i] = true
		r.mu.Unlock()
		if started {
			continue
		}
		{
			select {
			case r.slots <- struct{}{}:
			default:
				r.mu.Lock()
				delete(r.prefetched, i)
				r.mu.Unlock()
				return
			}
		}
		go func(i int) {
			defer func() { <-r.slots }()
			defer func() { recover() }() // a background prefetch must never take the mount down
			if _, err := r.chunk(context.Background(), nntp.Prefetch, i); err != nil {
				r.mu.Lock()
				delete(r.prefetched, i)
				r.mu.Unlock()
			}
		}(i)
	}
}

// readExact reads a body that should be exactly n bytes into one n-byte
// array (io.ReadAll grew it from 512 bytes: several copies per chunk and a
// cached array up to a quarter larger than its data). It returns what it
// got; len != n means the body was short, or longer than n (one byte past
// is read to tell).
func readExact(r io.Reader, n int64) ([]byte, error) {
	data := make([]byte, n)
	m, err := io.ReadFull(r, data)
	if err == io.ErrUnexpectedEOF || err == io.EOF {
		return data[:m], nil
	}
	if err != nil {
		return data[:m], err
	}
	var one [1]byte
	if k, _ := r.Read(one[:]); k > 0 {
		return append(data, one[0]), nil // too long: caller reports the size
	}
	return data, nil
}
