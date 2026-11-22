package stream

import (
	"bytes"
	"context"
	"math/rand"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Found live (Barbarian, 2026-11-22): a seek while a read at the old
// position was still waiting (the kernel's read-ahead, a second reader)
// paused the old stream under that read. It never asked again, waited for
// the paused stream to idle out, then restarted it, pausing the new
// position's stream: the two starved each other until the 120 s read
// timeout (a 104 s first byte after a seek). A waiting read now keeps the
// stream going to the end of its own bytes; read-ahead past them stays
// paused.
func TestSeekDoesNotStallReadAtOldPosition(t *testing.T) {
	file := make([]byte, 64<<20)
	rand.New(rand.NewSource(3)).Read(file)
	cdn := httptest.NewServer(serveRanges(file, 4<<20, nil)) // 4 MB/s
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "SEEK", int64(len(file)), ReadAhead{Bytes: 8 << 20})

	type res struct {
		n   int
		err error
		at  time.Duration
	}
	start := time.Now()
	old := make(chan res, 1)
	go func() {
		buf := make([]byte, 2<<20) // still downloading when the seek comes
		n, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 0)
		if err == nil && !bytes.Equal(buf[:n], file[:n]) {
			t.Error("old-position read: bytes differ")
		}
		old <- res{n, err, time.Since(start)}
	}()
	time.Sleep(150 * time.Millisecond)
	buf := make([]byte, 64<<10)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	n, err := r.ReadAtCtx(ctx, nntp.Live, buf, 40<<20)
	seekTook := time.Since(start)
	if err != nil || !bytes.Equal(buf[:n], file[40<<20:40<<20+n]) {
		t.Fatalf("seek read: n=%d err=%v", n, err)
	}
	select {
	case o := <-old:
		if o.err != nil || o.n != 2<<20 {
			t.Fatalf("old-position read: n=%d err=%v", o.n, o.err)
		}
		t.Logf("old-position read %s", o.at)
		if o.at > 3*time.Second {
			t.Fatalf("old-position read took %s: starved by the seek", o.at)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("old-position read never finished")
	}
	t.Logf("seek read %s", seekTook)
	if seekTook > 3*time.Second {
		t.Fatalf("seek read took %s: starved by the read at the old position", seekTook)
	}
}
