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
	if want := int((l.Size + l.PartSize - 1) / l.PartSize); want != file.Total {
		return Layout{}, fmt.Errorf("%w: %s: %d bytes in %d-byte parts needs %d segments, NZB has %d",
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

	mu         sync.Mutex
	prefetched map[int]bool // segments a read-ahead was already started for
}

// Open wraps file with a known layout. readAhead is how many segments to
// prefetch after each live read (0 disables it).
func Open(f Fetcher, cache *Cache, file nzb.File, l Layout, readAhead int) *File {
	by := make(map[int]nzb.Segment, len(file.Segments))
	for _, s := range file.Segments {
		by[s.Number] = s
	}
	return &File{name: file.Name, fetch: f, cache: cache, layout: l, byNumber: by,
		readAhead: readAhead, prefetched: map[int]bool{}}
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
	if prio == nntp.Live && r.readAhead > 0 && last >= 0 {
		r.startReadAhead(last + 1)
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
	return r.cache.DoCtx(ctx, seg.ID, prio, func(ctx context.Context) ([]byte, error) {
		data, meta, err := r.fetch.Body(ctx, prio, seg.ID)
		if err != nil {
			return nil, &SegmentError{File: r.name, Number: idx + 1, ID: seg.ID, Err: err}
		}
		wantOff, wantLen := int64(idx)*r.layout.PartSize, r.partLen(idx)
		// Single-part posts may omit =ypart; then only the length can be checked.
		offOK := meta.Offset == wantOff || (meta.Offset == 0 && meta.PartSize == 0)
		if !offOK || int64(len(data)) != wantLen {
			return nil, &SegmentError{File: r.name, Number: idx + 1, ID: seg.ID,
				Err: fmt.Errorf("%w: got offset %d len %d, want offset %d len %d",
					ErrLayout, meta.Offset, len(data), wantOff, wantLen)}
		}
		return data, nil
	})
}

// startReadAhead prefetches the next readAhead segments in the background.
func (r *File) startReadAhead(from int) {
	total := int((r.layout.Size + r.layout.PartSize - 1) / r.layout.PartSize)
	for idx := from; idx < from+r.readAhead && idx < total; idx++ {
		r.mu.Lock()
		started := r.prefetched[idx]
		r.prefetched[idx] = true
		r.mu.Unlock()
		if started {
			continue
		}
		seg, ok := r.byNumber[idx+1]
		if !ok {
			continue
		}
		if _, cached := r.cache.Get(seg.ID); cached {
			continue
		}
		go func(idx int) {
			if _, err := r.segment(context.Background(), nntp.Prefetch, idx); err != nil {
				// A failed prefetch may be retried by a later read-ahead.
				r.mu.Lock()
				delete(r.prefetched, idx)
				r.mu.Unlock()
			}
		}(idx)
	}
}
