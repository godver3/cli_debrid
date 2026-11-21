// Package stream serves byte ranges of usenet-posted files.
package stream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

// Fetcher downloads one decoded article. *nntp.Client implements it.
type Fetcher interface {
	Body(ctx context.Context, prio nntp.Priority, id string) ([]byte, nntp.Meta, error)
}

// Layout is how a file is split: every part is PartSize bytes except the
// last. It comes from the first segment's yEnc header and is stored so a
// file can be reopened without a network round trip.
type Layout struct {
	Size     int64  `json:"size"`
	PartSize int64  `json:"part_size"`
	YencName string `json:"yenc_name,omitempty"` // filename from the yEnc header
}

// ErrLayout means a segment did not sit where the layout says: the server
// returned a different article, or the poster used uneven part sizes.
var ErrLayout = errors.New("stream: segment layout mismatch")

// ErrNotInNZB means the NZB itself lacks a segment. It wraps
// nntp.ErrArticleNotFound: the data cannot be fetched from anywhere.
var ErrNotInNZB = fmt.Errorf("segment not listed in NZB: %w", nntp.ErrArticleNotFound)

// SegmentError says which segment failed. errors.Is(err,
// nntp.ErrArticleNotFound) tells proof of absence from transient trouble.
type SegmentError struct {
	File   string
	Number int // 1-based
	ID     string
	Err    error
}

func (e *SegmentError) Error() string {
	return fmt.Sprintf("stream: %s segment %d (%s): %v", e.File, e.Number, e.ID, e.Err)
}

func (e *SegmentError) Unwrap() error { return e.Err }

// Probe works out a file's layout by fetching its first segment (which is
// then cached, so the first read costs nothing extra).
func Probe(ctx context.Context, f Fetcher, cache *Cache, file nzb.File) (Layout, error) {
	if len(file.Segments) == 0 || file.Segments[0].Number != 1 {
		return Layout{}, &SegmentError{File: file.Name, Number: 1, Err: ErrNotInNZB}
	}
	first := file.Segments[0]
	var meta nntp.Meta
	data, err := cache.DoCtx(ctx, first.ID, nntp.Live, func(ctx context.Context) ([]byte, error) {
		d, m, err := f.Body(ctx, nntp.Live, first.ID)
		meta = m
		return d, err
	})
	if err != nil {
		return Layout{}, &SegmentError{File: file.Name, Number: 1, ID: first.ID, Err: err}
	}
	if meta.FileSize == 0 { // served from cache by a concurrent Probe; refetch header cheaply
		_, meta, err = f.Body(ctx, nntp.Live, first.ID)
		if err != nil {
			return Layout{}, &SegmentError{File: file.Name, Number: 1, ID: first.ID, Err: err}
		}
	}
	l := Layout{Size: meta.FileSize, PartSize: meta.PartSize, YencName: meta.FileName}
	if l.PartSize <= 0 {
		l.PartSize = int64(len(data))
	}
	if file.Total <= 1 {
		l.PartSize = l.Size
	}
	if l.Size <= 0 || l.PartSize <= 0 {
		return Layout{}, fmt.Errorf("%w: %s: no usable yEnc size (size=%d part=%d)", ErrLayout, file.Name, l.Size, l.PartSize)
	}
	// The yEnc header is real data; the subject's "(1/N)" isn't (see
	// nzb.File.Announced). A shorter NZB is missing its tail segments.
	if want := int((l.Size + l.PartSize - 1) / l.PartSize); want != file.Total {
		return Layout{}, fmt.Errorf("%w: %s: %d bytes in %d-byte parts needs %d segments, NZB lists %d",
			ErrLayout, file.Name, l.Size, l.PartSize, want, file.Total)
	}
	return l, nil
}

// File reads a posted file at any offset. Safe for concurrent use.
type File struct {
	name      string
	fetch     Fetcher
	cache     *Cache
	layout    Layout
	byNumber  map[int]nzb.Segment
	readAhead int
	slots     chan struct{} // caps concurrent prefetches for this file; nil = no cap
	// miss: which providers keep lacking this file's articles (Balanced
	// load mode asks them last).
	miss nntp.MissHint

	mu         sync.Mutex
	prefetched map[int]bool // segments a read-ahead was already started for
}

// ReadAhead says how much to prefetch after each live read.
type ReadAhead struct {
	Segments int   // articles ahead (used when Bytes is 0)
	Bytes    int64 // bytes ahead, rounded up to whole articles
	MaxConns int   // prefetches running at once for one file (0 = no cap)
}

func (ra ReadAhead) segments(partSize int64) int {
	if ra.Bytes > 0 && partSize > 0 {
		return int((ra.Bytes + partSize - 1) / partSize)
	}
	return ra.Segments
}

// Open wraps file with a known layout. A zero ReadAhead disables prefetch.
func Open(f Fetcher, cache *Cache, file nzb.File, l Layout, ra ReadAhead) *File {
	by := make(map[int]nzb.Segment, len(file.Segments))
	for _, s := range file.Segments {
		by[s.Number] = s
	}
	r := &File{name: file.Name, fetch: f, cache: cache, layout: l, byNumber: by,
		readAhead: ra.segments(l.PartSize), prefetched: map[int]bool{}}
	if ra.MaxConns > 0 {
		r.slots = make(chan struct{}, ra.MaxConns)
	}
	return r
}

// Size is the decoded file size.
func (r *File) Size() int64 { return r.layout.Size }

// ReadAt implements io.ReaderAt as a live read.
func (r *File) ReadAt(p []byte, off int64) (int, error) {
	return r.ReadAtCtx(context.Background(), nntp.Live, p, off)
}

// ReadAtCtx reads len(p) bytes at off with the given priority.
func (r *File) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("stream: negative offset")
	}
	if off >= r.layout.Size {
		return 0, io.EOF
	}
	n := 0
	last := -1
	for n < len(p) && off < r.layout.Size {
		idx := int(off / r.layout.PartSize)
		data, err := r.segment(ctx, prio, idx)
		if err != nil {
			return n, err
		}
		c := copy(p[n:], data[off-int64(idx)*r.layout.PartSize:])
		n += c
		off += int64(c)
		last = idx
	}
	if prio != nntp.Prefetch && r.readAhead > 0 && last >= 0 {
		n, owner := aheadFor(ctx, r.readAhead, r.layout.PartSize)
		r.startReadAhead(owner, last+1, n)
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func (r *File) partLen(idx int) int64 {
	start := int64(idx) * r.layout.PartSize
	if rest := r.layout.Size - start; rest < r.layout.PartSize {
		return rest
	}
	return r.layout.PartSize
}

// segment returns decoded segment idx (0-based), checked against the layout.
func (r *File) segment(ctx context.Context, prio nntp.Priority, idx int) ([]byte, error) {
	seg, ok := r.byNumber[idx+1]
	if !ok {
		return nil, &SegmentError{File: r.name, Number: idx + 1, Err: ErrNotInNZB}
	}
	wantOff, wantLen := int64(idx)*r.layout.PartSize, r.partLen(idx)
	// Single-part posts may omit =ypart; then only the length can be checked.
	fits := func(data []byte, meta nntp.Meta) bool {
		offOK := meta.Offset == wantOff || (meta.Offset == 0 && meta.PartSize == 0)
		return offOK && int64(len(data)) == wantLen
	}
	return r.cache.DoCtx(ctx, seg.ID, prio, func(ctx context.Context) ([]byte, error) {
		// A server whose copy doesn't fit is skipped for the next one.
		data, meta, err := fetchBody(nntp.WithMissHint(ctx, &r.miss), r.fetch, prio, seg.ID, fits)
		if errors.Is(err, nntp.ErrMismatch) {
			return nil, &SegmentError{File: r.name, Number: idx + 1, ID: seg.ID,
				Err: fmt.Errorf("%w: no server has a copy at offset %d len %d (%v)", ErrLayout, wantOff, wantLen, err)}
		}
		if err != nil {
			return nil, &SegmentError{File: r.name, Number: idx + 1, ID: seg.ID, Err: err}
		}
		if !fits(data, meta) {
			return nil, &SegmentError{File: r.name, Number: idx + 1, ID: seg.ID,
				Err: fmt.Errorf("%w: got offset %d len %d, want offset %d len %d",
					ErrLayout, meta.Offset, len(data), wantOff, wantLen)}
		}
		return data, nil
	})
}

// startReadAhead prefetches the next n segments in the background, until
// owner is done.
func (r *File) startReadAhead(owner context.Context, from, n int) {
	if owner.Err() != nil {
		return
	}
	total := int((r.layout.Size + r.layout.PartSize - 1) / r.layout.PartSize)
	for idx := from; idx < from+n && idx < total; idx++ {
		seg, ok := r.byNumber[idx+1]
		if !ok {
			continue
		}
		if _, cached := r.cache.Get(seg.ID); cached {
			continue
		}
		r.mu.Lock()
		started := r.prefetched[idx]
		r.prefetched[idx] = true
		r.mu.Unlock()
		if started {
			continue
		}
		if r.slots != nil {
			select {
			case r.slots <- struct{}{}:
			default:
				// Per-file cap reached: leave the rest for the next read-ahead.
				r.mu.Lock()
				delete(r.prefetched, idx)
				r.mu.Unlock()
				return
			}
		}
		go func(idx int) {
			if r.slots != nil {
				defer func() { <-r.slots }()
			}
			defer func() { recover() }() // a background prefetch must never take the mount down
			if _, err := r.segment(owner, nntp.Prefetch, idx); err != nil {
				// A failed prefetch may be retried by a later read-ahead.
				r.mu.Lock()
				delete(r.prefetched, idx)
				r.mu.Unlock()
			}
		}(idx)
	}
}
