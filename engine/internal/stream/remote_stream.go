package stream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Streaming downloaders for debrid playback, the way cli_mount (and rclone's
// VFS) do it: one sequential HTTP stream per read position, started exactly
// where the player reads, whose bytes are usable the moment they arrive. A
// seek starts a new stream and pauses the old one, so the bytes being watched
// get the whole line instead of a share of several parallel chunk requests.
// Completed chunks still land in the cache (memory + disk).

const (
	// streamWindow: a read this close ahead of a stream waits for it rather
	// than starting another one.
	streamWindow = 4 << 20
	// streamIdle: a paused stream nobody needs closes after this.
	streamIdle = 5 * time.Second
	// maxStreams per file (e.g. Plex playing while a scanner probes).
	maxStreams = 3
	// streamBlock is how much is read from the body between hand-offs.
	streamBlock = 64 << 10
	// streamRetries: reconnects in a row before a stream gives up.
	streamRetries = 4
)

// streamFailHold: after a stream gives up, reads of the same file fail
// with its error for this long before a new stream tries again (cli_mount
// answers a refused link at once too). A var for tests.
var streamFailHold = 30 * time.Second

var errStreamStopped = errors.New("stream: downloader stopped")

// downloader is one sequential HTTP stream. Guarded by RemoteFile.smu.
type downloader struct {
	r       *RemoteFile
	chunk   int    // chunk being filled
	bufFrom int64  // absolute offset of buf[0]
	buf     []byte // bytes [bufFrom, pos) of chunk
	pos     int64  // next byte to download
	// The chunk finished last, kept so a reader that wakes up late still
	// finds its bytes (a chunk started mid-way is not cached).
	prevChunk int
	prevFrom  int64
	prevBuf   []byte
	target    int64 // download up to here (exclusive), then pause
	lastUsed  time.Time
	err       error
	done      bool
	cancel    context.CancelFunc
}

// covers reports whether off is already in the partial buffer or the
// stream will reach it soon (caller holds smu).
func (d *downloader) covers(off int64) bool {
	if d.done {
		return false
	}
	inPrev := d.prevBuf != nil && off >= d.prevFrom && off < d.prevFrom+int64(len(d.prevBuf))
	return inPrev || (off >= d.bufFrom && off < d.pos) || (off >= d.pos && off-d.pos <= streamWindow)
}

// readStream serves a live read through the downloaders.
func (r *RemoteFile) readStream(ctx context.Context, p []byte, off int64) (int, error) {
	n := 0
	for n < len(p) && off+int64(n) < r.size {
		cur := off + int64(n)
		i := int(cur / r.chunkSize)
		if data, ok := r.cached(i); ok {
			n += copy(p[n:], data[cur-int64(i)*r.chunkSize:])
			continue
		}
		if r.cache.Started(r.key(i)) { // a pre-cache/prewarm fetch is on the wire: join it
			data, err := r.chunk(ctx, nntp.Live, i)
			if err != nil {
				return n, err
			}
			n += copy(p[n:], data[cur-int64(i)*r.chunkSize:])
			continue
		}
		want := min(off+int64(len(p)), r.size, int64(i+1)*r.chunkSize) // within this chunk
		got, err := r.waitStream(ctx, cur, want, p[n:])
		n += got
		if err != nil {
			return n, err
		}
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

// cached returns chunk i from memory or disk.
func (r *RemoteFile) cached(i int) ([]byte, bool) {
	if d, ok := r.cache.Get(r.key(i)); ok && int64(len(d)) == r.chunkLen(i) {
		return d, true
	}
	if r.cache.disk != nil {
		if d, ok := r.cache.disk.Get(r.key(i)); ok && int64(len(d)) == r.chunkLen(i) {
			r.cache.Put(r.key(i), d)
			return d, true
		}
	}
	return nil, false
}

// waitStream copies bytes [from, to) into dst once a downloader has them,
// starting or extending one as needed.
func (r *RemoteFile) waitStream(ctx context.Context, from, to int64, dst []byte) (int, error) {
	r.smu.Lock()
	defer r.smu.Unlock()
	if r.failErr != nil {
		if time.Since(r.failAt) < streamFailHold {
			return 0, r.failErr
		}
		r.failErr = nil
	}
	ahead := aheadBytesFor(ctx, r.aheadBytes())
	d := r.downloaderFor(from)
	d.lastUsed = time.Now()
	d.target = max(d.target, min(to+ahead, r.size))
	r.scond.Broadcast()
	for {
		i := int(from / r.chunkSize)
		if from >= d.bufFrom && to <= d.pos && d.chunk == i {
			return copy(dst, d.buf[from-d.bufFrom:to-d.bufFrom]), nil
		}
		if d.prevChunk == i && d.prevBuf != nil && from >= d.prevFrom && to <= d.prevFrom+int64(len(d.prevBuf)) {
			return copy(dst, d.prevBuf[from-d.prevFrom:to-d.prevFrom]), nil
		}
		if d.chunk > i || d.done {
			// The stream moved past this chunk (it is cached now) or stopped.
			r.smu.Unlock()
			data, ok := r.cached(i)
			r.smu.Lock()
			if ok {
				return copy(dst, data[from-int64(i)*r.chunkSize:to-int64(i)*r.chunkSize]), nil
			}
			if d.done && d.err != nil {
				return 0, d.err
			}
			// Gone past these bytes without keeping them, or stopped: start
			// a stream here.
			d = r.downloaderFor(from)
			d.lastUsed = time.Now()
			d.target = max(d.target, min(to+ahead, r.size))
			r.scond.Broadcast()
			continue
		}
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		r.scond.Wait()
	}
}

func (r *RemoteFile) aheadBytes() int64 {
	return max(int64(r.readAhead)*r.chunkSize, 2*r.chunkSize)
}

// downloaderFor returns a stream that will serve off, starting one at off
// (and pausing the others: this reader seeked away) if none is close.
func (r *RemoteFile) downloaderFor(off int64) *downloader {
	live := r.streams[:0]
	var best *downloader
	for _, d := range r.streams {
		if d.done {
			continue
		}
		live = append(live, d)
		if best == nil && d.covers(off) {
			best = d
		}
	}
	r.streams = live
	if best != nil {
		return best
	}
	for _, d := range r.streams { // pause the rest: the bytes now wanted come first
		d.target = d.pos
	}
	if len(r.streams) >= maxStreams { // retire the least recently used
		oldest := r.streams[0]
		for _, d := range r.streams {
			if d.lastUsed.Before(oldest.lastUsed) {
				oldest = d
			}
		}
		oldest.stop()
	}
	ctx, cancel := context.WithCancel(context.Background())
	d := &downloader{r: r, chunk: int(off / r.chunkSize), bufFrom: off, pos: off, target: off,
		lastUsed: time.Now(), cancel: cancel}
	r.streams = append(r.streams, d)
	go d.run(ctx)
	return d
}

// stop ends a stream (caller holds smu).
func (d *downloader) stop() {
	d.done = true
	d.cancel()
	d.r.scond.Broadcast()
}

// run downloads from pos until paused for streamIdle, the end, or errors.
func (d *downloader) run(ctx context.Context) {
	r := d.r
	defer func() {
		r.smu.Lock()
		d.done = true
		d.cancel()
		// Let go of the buffers: a stopped stream stays in r.streams until
		// the file's next seek, and the file stays built while its job is
		// open, so a played pack kept a chunk or two per file it ever read
		// (found live: 684 MB with nothing playing, the heap over the GC
		// limit). Completed chunks are in the cache; waitStream falls back
		// to it, or starts a new stream, once it sees done.
		d.buf, d.prevBuf = nil, nil
		d.bufFrom = d.pos
		r.scond.Broadcast()
		r.smu.Unlock()
	}()
	fails, relinked := 0, false
	for {
		// Wait while paused (pos reached target), up to streamIdle.
		r.smu.Lock()
		idleSince := time.Now()
		for d.pos >= d.target && !d.done && d.pos < r.size {
			if time.Since(idleSince) > streamIdle {
				r.smu.Unlock()
				return
			}
			r.waitTimeout(time.Second)
		}
		if d.done || d.pos >= r.size {
			r.smu.Unlock()
			return
		}
		start := d.pos
		r.smu.Unlock()

		status, err := d.pump(ctx, start)
		if ctx.Err() != nil {
			return
		}
		var dead *DeadFileError
		if errors.As(err, &dead) {
			d.fail(err) // the service proved it gone: retrying can't help
			return
		}
		if err == nil {
			fails, relinked = 0, false
			continue // paused or reached the end; loop decides
		}
		r.smu.Lock()
		progressed := d.pos > start
		r.smu.Unlock()
		if progressed {
			fails = 0
		}
		if status >= 400 && status < 500 && !transientStatus(status) {
			if relinked {
				d.fail(err)
				return
			}
			relinked = true
			if _, lerr := r.links.Link(ctx, r.ref, true); lerr != nil {
				d.fail(fmt.Errorf("stream: new link for %s: %w", r.ref, lerr))
				return
			}
			continue
		}
		if fails++; fails > streamRetries {
			if status == http.StatusServiceUnavailable && !relinked {
				// Still 503: ask the service for a fresh link once; a dead
				// file comes back as its own error.
				relinked = true
				if _, lerr := r.links.Link(ctx, r.ref, true); lerr != nil {
					d.fail(fmt.Errorf("stream: new link for %s: %w", r.ref, lerr))
					return
				}
				fails = 0
				continue
			}
			d.fail(err)
			return
		}
		select { // connection reset / 5xx: reconnect after a pause
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(250<<fails) * time.Millisecond):
		}
	}
}

func (d *downloader) fail(err error) {
	d.r.smu.Lock()
	d.err = err
	d.r.failErr, d.r.failAt = err, time.Now()
	d.r.smu.Unlock()
}

// waitTimeout waits on scond for at most dur (caller holds smu).
func (r *RemoteFile) waitTimeout(dur time.Duration) {
	t := time.AfterFunc(dur, func() {
		r.smu.Lock()
		r.scond.Broadcast()
		r.smu.Unlock()
	})
	r.scond.Wait()
	t.Stop()
}

// pump reads one HTTP response from start onward into the buffer until it
// is paused (pos reached target), the file ends or the body breaks. A nil
// error means it stopped cleanly.
func (d *downloader) pump(parent context.Context, start int64) (int, error) {
	r := d.r
	ctx, wd := newWatchdog(parent)
	defer wd.stop()
	st := countersFor(r.ref)
	url, err := r.links.Link(ctx, r.ref, false)
	if err != nil {
		st.linkFail(err)
		return 0, fmt.Errorf("stream: link for %s: %w", r.ref, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	// Ask for what is wanted now plus a margin, not the rest of the file:
	// an open-ended response keeps arriving into socket buffers while the
	// stream waits, megabytes nobody may read (a probe). Playback asks for
	// more as it goes; the next response starts where this one ended.
	r.smu.Lock()
	end := min(max(d.target, start)+streamWindow, r.size) - 1
	r.smu.Unlock()
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, end))
	if r.userAgent != "" {
		req.Header.Set("User-Agent", r.userAgent)
	}
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
		return 0, err
	}
	wd.progress()
	st.headers(began)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPartialContent && !(resp.StatusCode == http.StatusOK && start == 0) {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		err := fmt.Errorf("%w: HTTP %d from byte %d", ErrLinkRefused, resp.StatusCode, start)
		st.fail(err)
		return resp.StatusCode, err
	}
	block := make([]byte, streamBlock)
	for {
		r.smu.Lock()
		if d.done {
			r.smu.Unlock()
			return 0, errStreamStopped
		}
		if d.pos >= r.size || d.pos > end {
			r.smu.Unlock()
			return resp.StatusCode, nil // the end, or of this response's range
		}
		if d.pos >= d.target {
			// Far enough ahead: hold the connection (TCP pushes back) and
			// wait for the reader, like cli_mount; close it after streamIdle.
			idleSince := time.Now()
			wd.pause() // waiting for the reader on purpose, not a stall
			for d.pos >= d.target && !d.done && time.Since(idleSince) < streamIdle {
				r.waitTimeout(time.Second)
			}
			wd.resume()
			if d.done {
				r.smu.Unlock()
				return 0, errStreamStopped
			}
			if d.pos >= d.target {
				r.smu.Unlock()
				return resp.StatusCode, nil // idle: drop the connection
			}
		}
		chunkEnd := min(int64(d.chunk+1)*r.chunkSize, r.size)
		want := min(int64(len(block)), chunkEnd-d.pos, end+1-d.pos)
		r.smu.Unlock()

		m, err := io.ReadFull(progressReader{r: resp.Body, w: wd}, block[:want])
		st.bytes.Add(int64(m))
		r.smu.Lock()
		if m > 0 {
			if d.buf == nil {
				// Sized once for the rest of this chunk: growing by 64 KB
				// appends copied each chunk several times over (GC churn)
				// and left the cached array up to a quarter larger than
				// its bytes.
				end := min(int64(d.chunk+1)*r.chunkSize, r.size)
				d.buf = make([]byte, 0, max(end-d.bufFrom, int64(m)))
			}
			d.buf = append(d.buf, block[:m]...)
			d.pos += int64(m)
			r.failErr = nil
			if d.pos == chunkEnd { // chunk complete: cache it (whole chunks only), move on
				if d.bufFrom == int64(d.chunk)*r.chunkSize {
					r.cache.Put(r.key(d.chunk), d.buf)
				}
				d.prevChunk, d.prevFrom, d.prevBuf = d.chunk, d.bufFrom, d.buf
				d.chunk++
				d.bufFrom, d.buf = d.pos, nil
				if _, ok := r.cachedLocked(d.chunk); ok && d.pos < r.size {
					// Already cached: skip ahead instead of downloading it again.
					r.skipCachedLocked(d)
					r.scond.Broadcast()
					r.smu.Unlock()
					return resp.StatusCode, nil
				}
			}
			r.scond.Broadcast()
		}
		r.smu.Unlock()
		if err != nil {
			if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF) {
				r.smu.Lock()
				ended := d.pos >= r.size || d.pos > end
				r.smu.Unlock()
				if ended {
					return resp.StatusCode, nil
				}
				err := fmt.Errorf("stream: body ended early at %d", d.pos)
				st.fail(err)
				return 0, err
			}
			if wd.stalled() {
				err = fmt.Errorf("stream: no data for %s at %d: %w", remoteStallTimeout(), d.pos, err)
			}
			if parent.Err() == nil {
				st.fail(err)
			}
			return 0, err
		}
	}
}

// cachedLocked checks memory only (cheap, safe under smu).
func (r *RemoteFile) cachedLocked(i int) ([]byte, bool) {
	d, ok := r.cache.Get(r.key(i))
	return d, ok && int64(len(d)) == r.chunkLen(i)
}

// skipCachedLocked moves a stream past chunks already in memory.
func (r *RemoteFile) skipCachedLocked(d *downloader) {
	for d.pos < r.size {
		if _, ok := r.cachedLocked(d.chunk); !ok {
			break
		}
		d.chunk++
		d.pos = min(int64(d.chunk)*r.chunkSize, r.size)
		d.bufFrom, d.buf = d.pos, nil
	}
	d.target = max(d.target, d.pos)
}
