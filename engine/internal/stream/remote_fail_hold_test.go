package stream

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Found live 2026-11-21: a debrid file the CDN kept answering 503 took ~15 s
// per read to fail (reconnects + one fresh link), and a player's 128 KB reads
// each paid it again: a 1 MB read waited minutes. After a stream gives up,
// reads of that file fail at once for streamFailHold.

type countingLinks struct {
	url string
	n   atomic.Int64
	err error
}

func (c *countingLinks) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	c.n.Add(1)
	return c.url, c.err
}

func TestRefusedFileFailsFastAfterFirstGiveUp(t *testing.T) {
	old := streamFailHold
	streamFailHold = 300 * time.Millisecond
	t.Cleanup(func() { streamFailHold = old })
	var refuse atomic.Bool
	refuse.Store(true)
	var hits atomic.Int64
	file := make([]byte, 1<<20)
	ok := serveRanges(file, 0, nil)
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if refuse.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		ok(w, r)
	}))
	defer cdn.Close()
	r := OpenRemote(&countingLinks{url: cdn.URL}, nil, NewCache(64<<20), "R", int64(len(file)), ReadAhead{})
	buf := make([]byte, 128<<10)

	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 0); !errors.Is(err, ErrLinkRefused) {
		t.Fatalf("first read: want ErrLinkRefused, got %v", err)
	}
	first := hits.Load()
	start := time.Now()
	for i := 1; i < 8; i++ { // the rest of a 1 MB player read
		if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, int64(i)*int64(len(buf))); !errors.Is(err, ErrLinkRefused) {
			t.Fatalf("read %d: want the held error, got %v", i, err)
		}
	}
	if d := time.Since(start); d > 100*time.Millisecond {
		t.Fatalf("held reads took %v, want immediate", d)
	}
	if hits.Load() != first {
		t.Fatalf("held reads hit the CDN %d more times", hits.Load()-first)
	}

	time.Sleep(350 * time.Millisecond) // the hold is over and the CDN recovered
	refuse.Store(false)
	if n, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 0); err != nil || n != len(buf) {
		t.Fatalf("after the hold the file must read again: n=%d err=%v", n, err)
	}
}

func TestDeadLinkStopsTheStreamAtOnce(t *testing.T) {
	links := &countingLinks{err: &DeadFileError{Reason: "hoster_unavailable"}}
	r := OpenRemote(links, nil, NewCache(64<<20), "D", 1<<20, ReadAhead{})
	start := time.Now()
	_, err := r.ReadAtCtx(context.Background(), nntp.Live, make([]byte, 4096), 0)
	var dead *DeadFileError
	if !errors.As(err, &dead) {
		t.Fatalf("want DeadFileError, got %v", err)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("a proven-dead file took %v to fail (retried?)", d)
	}
	if links.n.Load() != 1 {
		t.Fatalf("asked for a dead link %d times, want 1", links.n.Load())
	}
}
