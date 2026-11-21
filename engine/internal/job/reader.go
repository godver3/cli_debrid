package job

import (
	"context"
	"fmt"
	"io"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// sourceReader reads one source (a posted file, or a mapped one).
type sourceReader interface {
	ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error)
}

// Files holds an open reader per source of a built job.
type Files struct {
	sources []sourceReader
}

// Open prepares readers for a stored job. n must be the same NZB the job
// was built from; no network traffic happens until something is read.
func Open(res *Result, n *nzb.NZB, f stream.Fetcher, cache *stream.Cache, readAhead stream.ReadAhead, links ...stream.LinkSource) (*Files, error) {
	out := &Files{}
	for _, s := range res.Sources {
		if s.Remote != "" {
			if len(links) == 0 || links[0] == nil {
				return nil, fmt.Errorf("job: %q is a debrid file but no link source is configured", s.Name)
			}
			out.sources = append(out.sources, stream.OpenRemote(links[0], nil, cache, s.Remote, s.Layout.Size, readAhead))
			continue
		}
		if n == nil {
			return nil, fmt.Errorf("job: source %q needs the NZB", s.Name)
		}
		if s.NZBIndex < 0 || s.NZBIndex >= len(n.Files) {
			return nil, fmt.Errorf("job: source %q points at NZB file %d of %d", s.Name, s.NZBIndex, len(n.Files))
		}
		if len(s.Map) > 0 {
			m := stream.OpenMapped(f, cache, n.Files[s.NZBIndex], s.Map, readAhead)
			if s.Encryption != nil && s.Encryption.Enabled() {
				d, err := stream.NewDecryptedFile(m, *s.Encryption, s.Layout.Size, m.Size())
				if err != nil {
					return nil, fmt.Errorf("job: source %q: %w", s.Name, err)
				}
				out.sources = append(out.sources, d)
				continue
			}
			out.sources = append(out.sources, m)
			continue
		}
		out.sources = append(out.sources, stream.Open(f, cache, n.Files[s.NZBIndex], s.Layout, readAhead))
	}
	return out, nil
}

// Entry returns a reader for one entry. An encrypted entry's pieces are
// read as ciphertext and decrypted.
func (fs *Files) Entry(e Entry) *EntryReader {
	r := &EntryReader{fs: fs, e: e}
	if e.Encryption == nil || !e.Encryption.Enabled() {
		return r
	}
	var cipherSize int64
	for _, pc := range e.Pieces {
		cipherSize += pc.Length
	}
	raw := e
	raw.Size, raw.Encryption = cipherSize, nil
	r.dec, r.decErr = stream.NewDecryptedFile(&EntryReader{fs: fs, e: raw}, *e.Encryption, e.Size, cipherSize)
	if r.decErr != nil {
		r.decErr = fmt.Errorf("job: %s: %w", e.Name, r.decErr)
	}
	return r
}

// Remote reports whether e is read from a debrid service (every piece is
// a remote source).
func (fs *Files) Remote(e Entry) bool {
	if len(e.Pieces) == 0 {
		return false
	}
	for _, pc := range e.Pieces {
		if pc.Source < 0 || pc.Source >= len(fs.sources) {
			return false
		}
		if _, ok := fs.sources[pc.Source].(*stream.RemoteFile); !ok {
			return false
		}
	}
	return true
}

// EntryReader reads an entry across its pieces.
type EntryReader struct {
	fs     *Files
	e      Entry
	dec    *stream.DecryptedFile // encrypted entries
	decErr error
}

// Size is the entry's size.
func (r *EntryReader) Size() int64 { return r.e.Size }

// ReadAt is a live read.
func (r *EntryReader) ReadAt(p []byte, off int64) (int, error) {
	return r.ReadAtCtx(context.Background(), nntp.Live, p, off)
}

// ReadAtCtx reads len(p) bytes at off with the given priority.
func (r *EntryReader) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if r.decErr != nil {
		return 0, r.decErr
	}
	if r.dec != nil {
		return r.dec.ReadAtCtx(ctx, prio, p, off)
	}
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
