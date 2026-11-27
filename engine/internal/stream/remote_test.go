package stream

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// serveRanges answers Range requests ("bytes=a-b" or "bytes=a-") from
// file, optionally throttled to bps bytes/second.
func serveRanges(file []byte, bps int, onReq func(r *http.Request)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if onReq != nil {
			onReq(r)
		}
		var a, b int64 = 0, int64(len(file)) - 1
		spec := strings.TrimPrefix(r.Header.Get("Range"), "bytes=")
		if parts := strings.SplitN(spec, "-", 2); len(parts) == 2 {
			fmt.Sscan(parts[0], &a)
			if parts[1] != "" {
				fmt.Sscan(parts[1], &b)
			}
		}
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(file)))
		w.Header().Set("Content-Length", fmt.Sprint(b-a+1))
		w.WriteHeader(http.StatusPartialContent)
		body := file[a : b+1]
		step := 32 << 10
		for i := 0; i < len(body); i += step {
			if _, err := w.Write(body[i:min(i+step, len(body))]); err != nil {
				return
			}
			if bps > 0 {
				w.(http.Flusher).Flush()
				time.Sleep(time.Duration(float64(step) / float64(bps) * float64(time.Second)))
			}
		}
	}
}

type staticLink string

func (s staticLink) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	return string(s), nil
}

// Found live: chunks cached by an older chunk size (same key) crashed the
// engine with a slice panic. Keys now carry the size, and a chunk of the
// wrong length is an error, never a panic.
func TestRemoteChunkCacheSafety(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (RemoteChunk+5000)/10)
	cdn := httptest.NewServer(serveRanges(file, 0, nil))
	defer cdn.Close()
	cache := NewCache(64 << 20)
	cache.put("remote:F:0", []byte("old 1 MiB chunk")) // the old key format
	r := OpenRemote(staticLink(cdn.URL), nil, cache, "F", int64(len(file)), ReadAhead{})
	buf := make([]byte, 100)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 1<<20+50); err != nil || !bytes.Equal(buf, file[1<<20+50:1<<20+150]) {
		t.Fatalf("read with a stale old-format entry cached: %v", err)
	}
	cache.put(r.key(1), []byte("short")) // a wrong-length chunk under the current key
	if _, err := r.ReadAtCtx(context.Background(), nntp.Prefetch, buf, RemoteChunk+10); err == nil || !strings.Contains(err.Error(), "want") {
		t.Fatalf("chunk fetch: a wrong-length chunk must be an error: %v", err)
	}
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, RemoteChunk+10); err != nil || !bytes.Equal(buf, file[RemoteChunk+10:RemoteChunk+110]) {
		t.Fatalf("playback must ignore a wrong-length cached chunk and stream the bytes: %v", err)
	}
}

// A live read for a chunk a read-ahead is already downloading joins it:
// each chunk is fetched once.
func TestRemoteSequentialPlaybackUsesOneStream(t *testing.T) {
	file := bytes.Repeat([]byte("abcdefghij"), (3*RemoteChunk+12345)/10)
	var reqs atomic.Int64
	cdn := httptest.NewServer(serveRanges(file, 0, func(q *http.Request) { reqs.Add(1); t.Log("request", q.Header.Get("Range")) }))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "S", int64(len(file)), ReadAhead{Bytes: 8 << 20})
	got := make([]byte, 0, len(file))
	buf := make([]byte, 128<<10)
	for off := int64(0); off < int64(len(file)); {
		n, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, off)
		got = append(got, buf[:n]...)
		off += int64(n)
		if err != nil && err != io.EOF {
			t.Fatal(err)
		}
		if n == 0 {
			break
		}
	}
	if !bytes.Equal(got, file) {
		t.Fatal("bytes differ")
	}
	if reqs.Load() != 1 {
		t.Fatalf("sequential playback made %d requests, want 1 stream", reqs.Load())
	}
	if _, ok := r.cache.Get(r.key(1)); !ok {
		t.Fatal("completed chunks should be cached")
	}
}

// After a seek the first bytes come as soon as the new stream starts (not
// after a whole chunk), and the old stream stops competing.
func TestRemoteSeekStartsAFreshStreamFast(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (8*RemoteChunk)/10)
	cdn := httptest.NewServer(serveRanges(file, 4<<20, nil)) // 4 MB/s
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "K", int64(len(file)), ReadAhead{Bytes: 16 << 20})
	buf := make([]byte, 128<<10)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	seek := int64(5*RemoteChunk + 777)
	start := time.Now()
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, seek); err != nil || !bytes.Equal(buf, file[seek:seek+int64(len(buf))]) {
		t.Fatalf("read after seek: %v", err)
	}
	if took := time.Since(start); took > 600*time.Millisecond {
		t.Fatalf("first bytes after a seek took %v; a whole 4 MiB chunk at 4 MB/s would be ~1 s", took)
	}
	r.smu.Lock()
	var first *downloader
	for _, d := range r.streams {
		if d.bufFrom < RemoteChunk || d.prevFrom < RemoteChunk && d.prevBuf != nil {
			first = d
		}
	}
	paused := first == nil || first.target <= first.pos
	r.smu.Unlock()
	if !paused {
		t.Fatal("the stream at the old position should be paused after the seek")
	}
}

// A dropped connection resumes from where it stopped, with the same link.
func TestRemoteStreamResumesAfterReset(t *testing.T) {
	file := bytes.Repeat([]byte("qwertyuiop"), (2*RemoteChunk)/10)
	var reqs atomic.Int64
	serve := serveRanges(file, 0, nil)
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if reqs.Add(1) == 1 { // first stream: send 1 MiB, then drop the connection
			w.Header().Set("Content-Range", fmt.Sprintf("bytes 0-%d/%d", len(file)-1, len(file)))
			w.Header().Set("Content-Length", fmt.Sprint(len(file)))
			w.WriteHeader(http.StatusPartialContent)
			w.Write(file[:1<<20])
			w.(http.Flusher).Flush()
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
			return
		}
		if !strings.HasPrefix(r.Header.Get("Range"), "bytes=1048576-") {
			t.Errorf("resumed at %q, want byte 1048576", r.Header.Get("Range"))
		}
		serve(w, r)
	}))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "R", int64(len(file)), ReadAhead{Bytes: 8 << 20})
	buf := make([]byte, 64<<10)
	off := int64(3 << 20)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, off); err != nil || !bytes.Equal(buf, file[off:off+int64(len(buf))]) {
		t.Fatalf("read past the reset: %v", err)
	}
}

type optLink struct {
	staticLink
	o RemoteOptions
}

func (l optLink) RemoteOptions() RemoteOptions { return l.o }

// Debrid streaming settings reach every file of a link source: range size,
// User-Agent, parallel ranges and read-ahead.
func TestRemoteOptionsFromLinkSource(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (3<<20)/10)
	var agents atomic.Value
	cdn := httptest.NewServer(serveRanges(file, 0, func(r *http.Request) { agents.Store(r.Header.Get("User-Agent")) }))
	defer cdn.Close()
	l := optLink{staticLink(cdn.URL), RemoteOptions{ChunkBytes: 1 << 20, MaxConns: 2, ReadAheadBytes: 2 << 20, UserAgent: "clid-test/1"}}
	r := OpenRemote(l, nil, NewCache(64<<20), "rd:O:1", int64(len(file)), ReadAhead{Bytes: 64 << 20})
	if r.chunkSize != 1<<20 || cap(r.slots) != 2 || r.readAhead != 2 {
		t.Fatalf("chunk=%d slots=%d ahead=%d", r.chunkSize, cap(r.slots), r.readAhead)
	}
	buf := make([]byte, 100)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Prefetch, buf, (1<<20)+5); err != nil || !bytes.Equal(buf, file[(1<<20)+5:(1<<20)+105]) {
		t.Fatalf("read: %v", err)
	}
	if ua, _ := agents.Load().(string); ua != "clid-test/1" {
		t.Fatalf("user agent %q", ua)
	}
	// without options: the defaults
	d := OpenRemote(staticLink(cdn.URL), nil, NewCache(64<<20), "rd:O:2", int64(len(file)), ReadAhead{})
	if d.chunkSize != RemoteChunk || cap(d.slots) != RemoteMaxConns {
		t.Fatalf("defaults: chunk=%d slots=%d", d.chunkSize, cap(d.slots))
	}
}

func TestRemoteRejectsWrongContentRange(t *testing.T) {
	file := bytes.Repeat([]byte("good"), 16)
	for _, mode := range []string{"chunk", "stream"} {
		for _, tc := range []struct {
			name, header string
		}{
			{"wrong start", "bytes 1-64/65"},
			{"wrong length", "bytes 0-62/64"},
			{"missing", ""},
		} {
			t.Run(mode+"/"+tc.name, func(t *testing.T) {
				var requests atomic.Int32
				serve := serveRanges(file, 0, nil)
				cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if requests.Add(1) == 1 {
						w.Header().Set("Content-Range", tc.header)
						w.WriteHeader(http.StatusPartialContent)
						w.Write(bytes.Repeat([]byte("x"), len(file)))
						return
					}
					serve(w, r)
				}))
				defer cdn.Close()
				links := &countLink{url: cdn.URL}
				r := OpenRemote(links, cdn.Client(), NewCache(RemoteChunk), t.Name(), int64(len(file)), ReadAhead{})
				ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
				defer cancel()
				var got []byte
				var err error
				if mode == "chunk" {
					got, err = r.chunk(ctx, nntp.Prefetch, 0)
				} else {
					got = make([]byte, len(file))
					_, err = r.ReadAtCtx(ctx, nntp.Live, got, 0)
				}
				if err != nil || !bytes.Equal(got, file) {
					t.Fatalf("read after invalid 206: %q, %v", got, err)
				}
				if links.refreshes.Load() != 1 || requests.Load() != 2 {
					t.Fatalf("refreshes=%d requests=%d, want 1 and 2", links.refreshes.Load(), requests.Load())
				}
			})
		}
	}
}
