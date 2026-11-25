package stream

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// A mount read's hint caps the read-ahead it starts (a probe reading the
// head must not pull the whole configured window).
func TestReadHintCapsReadAhead(t *testing.T) {
	_, _, s, c, f := setup(t, 7_000_000, 700_000) // 10 segments
	cache := NewCache(64 << 20)
	l, _ := Probe(context.Background(), c, cache, f) // fetches segment 1
	r := Open(c, cache, f, l, ReadAhead{Segments: 8})
	ctx := WithReadHint(context.Background(), ReadHint{Ahead: 700_000})
	if _, err := r.ReadAtCtx(ctx, 2 /* Live */, make([]byte, 1000), 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if got := s.Bodies.Load(); got != 2 { // probe + one segment of read-ahead
		t.Fatalf("fetched %d bodies, want 2 (hint allows one segment ahead)", got)
	}
	// Without a hint the configured read-ahead applies.
	if _, err := r.ReadAt(make([]byte, 1000), 0); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for s.Bodies.Load() < 9 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := s.Bodies.Load(); got != 9 {
		t.Fatalf("fetched %d bodies, want 9 (8 ahead without a hint)", got)
	}
}

// Read-ahead started for a handle stops when its owner (the handle) ends.
func TestReadHintOwnerCancelsQueuedReadAhead(t *testing.T) {
	_, _, s, c, f := setup(t, 7_000_000, 700_000) // 10 segments, client has 4 conns
	cache := NewCache(64 << 20)
	l, _ := Probe(context.Background(), c, cache, f)
	s.SetDelay(300 * time.Millisecond)
	r := Open(c, cache, f, l, ReadAhead{Segments: 9})
	owner, cancel := context.WithCancel(context.Background())
	ctx := WithReadHint(context.Background(), ReadHint{Owner: owner})
	if _, err := r.ReadAtCtx(ctx, 2, make([]byte, 1000), 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond) // 4 on the wire, 5 waiting for a connection
	cancel()
	time.Sleep(1500 * time.Millisecond)
	if got := s.Bodies.Load(); got > 1+4 {
		t.Fatalf("fetched %d bodies after the owner closed, want at most 5 (probe + those already on the wire)", got)
	}
	// A later read-ahead may fetch the cancelled segments again.
	if _, err := r.ReadAt(make([]byte, 1000), 0); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for s.Bodies.Load() < 10 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if got := s.Bodies.Load(); got < 10 {
		t.Fatalf("fetched %d bodies, want the cancelled segments fetched by the next read-ahead", got)
	}
}

// A probe-sized debrid read with a small hint downloads about that much,
// not the configured read-ahead (Bazarr probing every file filled the line).
func TestReadHintCapsDebridStream(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (8*RemoteChunk)/10)
	var served atomic.Int64
	counting := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		cw := &countWriter{ResponseWriter: w, n: &served}
		serveRanges(file, 16<<20, nil)(cw, req)
	}))
	defer counting.Close()
	defer counting.CloseClientConnections() // the paused stream holds its connection
	r := OpenRemote(staticLink(counting.URL), nil, NewCache(256<<20), "K", int64(len(file)), ReadAhead{Bytes: 32 << 20})
	ctx := WithReadHint(context.Background(), ReadHint{Ahead: 1 << 20})
	buf := make([]byte, 64<<10)
	if _, err := r.ReadAtCtx(ctx, nntp.Live, buf, 0); err != nil || !bytes.Equal(buf, file[:len(buf)]) {
		t.Fatalf("read: %v", err)
	}
	time.Sleep(time.Second)
	// The hint (1 MB) plus the request's margin (streamWindow), not the
	// 32 MB read-ahead nor the socket buffers of an open-ended request.
	if got := served.Load(); got > (1<<20)+streamWindow+(128<<10) {
		t.Fatalf("downloaded %d bytes for a 64 KB read with a 1 MB hint; want <= hint + %d", got, streamWindow)
	}
}

type countWriter struct {
	http.ResponseWriter
	n *atomic.Int64
}

func (c *countWriter) Write(p []byte) (int, error) {
	n, err := c.ResponseWriter.Write(p)
	c.n.Add(int64(n))
	return n, err
}

func (c *countWriter) Flush() { c.ResponseWriter.(http.Flusher).Flush() }
