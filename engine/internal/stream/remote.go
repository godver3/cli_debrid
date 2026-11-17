package stream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// RemoteChunk is the unit fetched from a debrid provider (one HTTP range).
const RemoteChunk = 1 << 20

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
	links     LinkSource
	cache     *Cache
	client    *http.Client
	readAhead int
	slots     chan struct{}

	mu         sync.Mutex
	prefetched map[int]bool
}

// OpenRemote wraps a debrid file of the given size.
func OpenRemote(links LinkSource, client *http.Client, cache *Cache, ref string, size int64, ra ReadAhead) *RemoteFile {
	if client == nil {
		client = http.DefaultClient
	}
	r := &RemoteFile{ref: ref, size: size, links: links, cache: cache, client: client,
		readAhead: ra.segments(RemoteChunk), prefetched: map[int]bool{}}
	if ra.MaxConns > 0 {
		r.slots = make(chan struct{}, ra.MaxConns)
	}
	return r
}

// Size is the file size.
func (r *RemoteFile) Size() int64 { return r.size }

func (r *RemoteFile) chunkLen(i int) int64 {
	return min(RemoteChunk, r.size-int64(i)*RemoteChunk)
}

func (r *RemoteFile) chunk(ctx context.Context, prio nntp.Priority, i int) ([]byte, error) {
	key := "remote:" + r.ref + ":" + strconv.Itoa(i)
	return r.cache.DoCtx(ctx, key, prio, func(ctx context.Context) ([]byte, error) {
		start, n := int64(i)*RemoteChunk, r.chunkLen(i)
		var last error
		for attempt := 0; attempt < 2; attempt++ { // second try with a fresh link
			url, err := r.links.Link(ctx, r.ref, attempt > 0)
			if err != nil {
				return nil, fmt.Errorf("stream: link for %s: %w", r.ref, err)
			}
			data, status, err := r.get(ctx, url, start, n)
			if err == nil {
				return data, nil
			}
			last = err
			if status == 0 || status >= 500 && status != 503 { // network trouble: no point re-linking
				break
			}
		}
		return nil, last
	})
}

func (r *RemoteFile) get(ctx context.Context, url string, start, n int64) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, start+n-1))
	resp, err := r.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusPartialContent:
	case resp.StatusCode == http.StatusOK && start == 0 && n == r.size:
	default:
		io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return nil, resp.StatusCode, fmt.Errorf("%w: HTTP %d for bytes %d-%d", ErrLinkRefused, resp.StatusCode, start, start+n-1)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, n+1))
	if err != nil {
		return nil, resp.StatusCode, err
	}
	if int64(len(data)) != n {
		return nil, resp.StatusCode, fmt.Errorf("stream: got %d bytes, want %d", len(data), n)
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
	n, last := 0, -1
	for n < len(p) && off+int64(n) < r.size {
		i := int((off + int64(n)) / RemoteChunk)
		data, err := r.chunk(ctx, prio, i)
		if err != nil {
			return n, err
		}
		n += copy(p[n:], data[off+int64(n)-int64(i)*RemoteChunk:])
		last = i
	}
	if prio == nntp.Live && r.readAhead > 0 && last >= 0 {
		r.startReadAhead(last + 1)
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func (r *RemoteFile) startReadAhead(from int) {
	total := int((r.size + RemoteChunk - 1) / RemoteChunk)
	for i := from; i < from+r.readAhead && i < total; i++ {
		if _, ok := r.cache.Get("remote:" + r.ref + ":" + strconv.Itoa(i)); ok {
			continue
		}
		r.mu.Lock()
		started := r.prefetched[i]
		r.prefetched[i] = true
		r.mu.Unlock()
		if started {
			continue
		}
		if r.slots != nil {
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
			if r.slots != nil {
				defer func() { <-r.slots }()
			}
			if _, err := r.chunk(context.Background(), nntp.Prefetch, i); err != nil {
				r.mu.Lock()
				delete(r.prefetched, i)
				r.mu.Unlock()
			}
		}(i)
	}
}
