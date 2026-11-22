package nntp

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

// On Windows the clock moves in timer ticks, so a loopback fetch can
// measure 0 ms.
func TestSpeedTestOneProviderOnly(t *testing.T) {
	arts := nntptest.Post("st", "f.bin", awkward(40000), 4000)
	a := nntptest.Start(t, arts...)
	b := nntptest.Start(t) // has nothing
	c, _ := NewClient([]Provider{provider(a, "a", 4), provider(b, "b", 4)})
	defer c.Close()
	ids := make([]string, 0, len(arts)+1)
	for _, x := range arts {
		ids = append(ids, x.ID)
	}
	ids = append(ids, "missing@x")

	res, err := c.SpeedTest(context.Background(), "a", ids, 3, 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Articles != len(arts) || res.Missing != 1 || res.Bytes != 40000 || res.Error != "" || res.LatencyMs < 0 || (res.LatencyMs == 0 && runtime.GOOS != "windows") {
		t.Fatalf("a: %+v", res)
	}
	// b is asked alone: it has none of them, and a is never consulted
	res, err = c.SpeedTest(context.Background(), "b", ids, 3, 0)
	if err != nil || res.Articles != 0 || res.Error == "" {
		t.Fatalf("b: %+v %v", res, err)
	}
	if _, err := c.SpeedTest(context.Background(), "nope", ids, 3, 0); !errors.Is(err, ErrUnknownProvider) {
		t.Fatalf("unknown provider: %v", err)
	}
	// byte budget stops early
	res, _ = c.SpeedTest(context.Background(), "a", ids, 1, 8000)
	if res.Articles != 2 {
		t.Fatalf("budget: %+v", res)
	}
}

// The first test after a quiet spell reuses idle connections the server
// already closed ("broken pipe"). That is not a failed test: the article is
// read again on a fresh connection and no error is reported.
func TestSpeedTestRetriesStaleIdleConnections(t *testing.T) {
	arts := nntptest.Post("st", "f.bin", awkward(40000), 4000)
	s := nntptest.Start(t, arts...)
	c, _ := NewClient([]Provider{provider(s, "a", 4)})
	defer c.Close()
	ids := make([]string, 0, len(arts))
	for _, x := range arts {
		ids = append(ids, x.ID)
	}
	if res, _ := c.SpeedTest(context.Background(), "a", ids[:4], 4, 0); res.Error != "" {
		t.Fatalf("warm-up: %+v", res)
	}
	pl := c.pools[0]
	pl.mu.Lock()
	if len(pl.idle) == 0 {
		pl.mu.Unlock()
		t.Fatal("want idle connections after the warm-up")
	}
	for _, cn := range pl.idle {
		cn.lastUsed = time.Now().Add(-time.Minute)
	}
	pl.mu.Unlock()
	s.CloseConns()
	time.Sleep(50 * time.Millisecond)

	res, err := c.SpeedTest(context.Background(), "a", ids, 4, 0)
	if err != nil || res.Error != "" || res.Articles != len(arts) {
		t.Fatalf("after the server closed idle connections: %+v %v", res, err)
	}
}
