package stream

import (
	"context"
	"fmt"
	"io"
	"sort"
	"sync"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

// MapSeg places one article's data in a file: file bytes [Start,
// Start+Len) are decoded-article bytes [DataStart, DataStart+Len). It is
// how cli_mount stores files (including archive members sliced out of
// volume articles), so jobs migrated from cli_mount stream byte-for-byte
// as cli_mount served them, without re-reading their archives.
type MapSeg struct {
	Start     int64 `json:"s"`
	Len       int64 `json:"l"`
	DataStart int64 `json:"d,omitempty"`
}

// MappedFile reads a file through a segment map. Segment i is article
// number i+1 of the NZB file it was opened with.
type MappedFile struct {
	name      string
	fetch     Fetcher
	cache     *Cache
	segs      []MapSeg
	ids       []string
	size      int64
	readAhead int
	slots     chan struct{}

	mu         sync.Mutex
	prefetched map[int]bool
}

// ValidateMap checks a map is contiguous from 0 and returns the file size.
func ValidateMap(segs []MapSeg) (int64, error) {
	var off int64
	for i, s := range segs {
		if s.Start != off || s.Len <= 0 || s.DataStart < 0 {
			return 0, fmt.Errorf("stream: segment %d maps [%d,+%d) at data %d, want start %d", i, s.Start, s.Len, s.DataStart, off)
		}
		off += s.Len
	}
	if len(segs) == 0 {
		return 0, fmt.Errorf("stream: empty segment map")
	}
	return off, nil
}

// OpenMapped wraps file (its articles in map order) with a segment map.
func OpenMapped(f Fetcher, cache *Cache, file nzb.File, segs []MapSeg, ra ReadAhead) *MappedFile {
	byNum := map[int]string{}
	for _, s := range file.Segments {
		byNum[s.Number] = s.ID
	}
	ids := make([]string, len(segs))
	for i := range segs {
		ids[i] = byNum[i+1]
	}
	var size, avg int64
	if n := len(segs); n > 0 {
		size = segs[n-1].Start + segs[n-1].Len
		avg = size / int64(n)
	}
	m := &MappedFile{name: file.Name, fetch: f, cache: cache, segs: segs, ids: ids, size: size,
		readAhead: ra.segments(avg), prefetched: map[int]bool{}}
	if ra.MaxConns > 0 {
		m.slots = make(chan struct{}, ra.MaxConns)
	}
	return m
}

// Size is the file size.
func (m *MappedFile) Size() int64 { return m.size }

func (m *MappedFile) find(off int64) int {
	return sort.Search(len(m.segs), func(i int) bool { return m.segs[i].Start+m.segs[i].Len > off })
}

// segment returns the file bytes of segment i.
func (m *MappedFile) segment(ctx context.Context, prio nntp.Priority, i int) ([]byte, error) {
	id := m.ids[i]
	if id == "" {
		return nil, &SegmentError{File: m.name, Number: i + 1, Err: ErrNotInNZB}
	}
	data, err := m.cache.DoCtx(ctx, id, prio, func(ctx context.Context) ([]byte, error) {
		d, _, err := m.fetch.Body(ctx, prio, id)
		if err != nil {
			return nil, &SegmentError{File: m.name, Number: i + 1, ID: id, Err: err}
		}
		return d, nil
	})
	if err != nil {
		return nil, err
	}
	s := m.segs[i]
	if int64(len(data)) < s.DataStart+s.Len {
		return nil, &SegmentError{File: m.name, Number: i + 1, ID: id,
			Err: fmt.Errorf("%w: article has %d bytes, map needs %d", ErrLayout, len(data), s.DataStart+s.Len)}
	}
	return data[s.DataStart : s.DataStart+s.Len], nil
}

// ReadAtCtx reads len(p) bytes at off with the given priority.
func (m *MappedFile) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("stream: negative offset")
	}
	if off >= m.size {
		return 0, io.EOF
	}
	n, last := 0, -1
	for i := m.find(off); n < len(p) && i < len(m.segs); i++ {
		data, err := m.segment(ctx, prio, i)
		if err != nil {
			return n, err
		}
		n += copy(p[n:], data[off+int64(n)-m.segs[i].Start:])
		last = i
	}
	if prio == nntp.Live && m.readAhead > 0 && last >= 0 {
		m.startReadAhead(last + 1)
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func (m *MappedFile) startReadAhead(from int) {
	for i := from; i < from+m.readAhead && i < len(m.segs); i++ {
		if m.ids[i] == "" {
			continue
		}
		if _, cached := m.cache.Get(m.ids[i]); cached {
			continue
		}
		m.mu.Lock()
		started := m.prefetched[i]
		m.prefetched[i] = true
		m.mu.Unlock()
		if started {
			continue
		}
		if m.slots != nil {
			select {
			case m.slots <- struct{}{}:
			default:
				m.mu.Lock()
				delete(m.prefetched, i)
				m.mu.Unlock()
				return
			}
		}
		go func(i int) {
			if m.slots != nil {
				defer func() { <-m.slots }()
			}
			if _, err := m.segment(context.Background(), nntp.Prefetch, i); err != nil {
				m.mu.Lock()
				delete(m.prefetched, i)
				m.mu.Unlock()
			}
		}(i)
	}
}
