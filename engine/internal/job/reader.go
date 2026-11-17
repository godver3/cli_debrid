package job

import (
	"context"
	"fmt"
	"io"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Files holds an open stream.File per source of a built job.
type Files struct {
	sources []*stream.File
}

// Open prepares readers for a stored job. n must be the same NZB the job
// was built from; no network traffic happens until something is read.
func Open(res *Result, n *nzb.NZB, f stream.Fetcher, cache *stream.Cache, readAhead int) (*Files, error) {
	out := &Files{}
	for _, s := range res.Sources {
		if s.NZBIndex < 0 || s.NZBIndex >= len(n.Files) {
			return nil, fmt.Errorf("job: source %q points at NZB file %d of %d", s.Name, s.NZBIndex, len(n.Files))
		}
		out.sources = append(out.sources, stream.Open(f, cache, n.Files[s.NZBIndex], s.Layout, readAhead))
	}
	return out, nil
}

// Entry returns a reader for one entry.
func (fs *Files) Entry(e Entry) *EntryReader { return &EntryReader{fs: fs, e: e} }

// EntryReader reads an entry across its pieces.
type EntryReader struct {
	fs *Files
	e  Entry
}

// Size is the entry's size.
func (r *EntryReader) Size() int64 { return r.e.Size }

// ReadAt is a live read.
func (r *EntryReader) ReadAt(p []byte, off int64) (int, error) {
	return r.ReadAtCtx(context.Background(), nntp.Live, p, off)
}

// ReadAtCtx reads len(p) bytes at off with the given priority.
func (r *EntryReader) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if off >= r.e.Size {
		return 0, io.EOF
	}
	n := 0
	pos := int64(0)
	for _, pc := range r.e.Pieces {
		if n == len(p) {
			break
		}
		end := pos + pc.Length
		if cur := off + int64(n); cur < end {
			in := cur - pos
			want := min(int64(len(p)-n), pc.Length-in)
			got, err := r.fs.sources[pc.Source].ReadAtCtx(ctx, prio, p[n:n+int(want)], pc.Offset+in)
			n += got
			if err != nil && !(err == io.EOF && int64(got) == want) {
				return n, err
			}
		}
		pos = end
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}
