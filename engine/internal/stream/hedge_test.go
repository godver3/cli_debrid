package stream

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// A live read stuck behind a stalled background fetch on the wire stops
// waiting after hedgeAfter and gets the article itself.
func TestLiveHedgesStalledPrefetch(t *testing.T) {
	old := hedgeMin
	hedgeMin = 50 * time.Millisecond
	defer func() { hedgeMin = old }()
	c := NewCache(1 << 20)
	release := make(chan struct{})
	defer close(release)
	go c.Do("k", nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
		nntp.FireAcquireHook(ctx)
		<-release // a stalled connection
		return []byte("slow"), nil
	})
	time.Sleep(20 * time.Millisecond)
	start := time.Now()
	d, err := c.Do("k", nntp.Live, func(context.Context) ([]byte, error) { return []byte("hedged"), nil })
	if err != nil || string(d) != "hedged" {
		t.Fatalf("got %q %v", d, err)
	}
	if took := time.Since(start); took > 300*time.Millisecond {
		t.Fatalf("hedge took %s", took)
	}
	if c.Hedges.Load() != 1 || c.HedgeWins.Load() != 1 {
		t.Fatalf("hedges %d wins %d", c.Hedges.Load(), c.HedgeWins.Load())
	}
	if got, ok := c.Get("k"); !ok || string(got) != "hedged" {
		t.Fatal("hedged article not cached")
	}
}

// A background fetch that finishes in normal time is joined, not doubled.
func TestLiveJoinsNormalPrefetchWithoutHedge(t *testing.T) {
	c := NewCache(1 << 20)
	var fetches atomic.Int32
	go c.Do("k", nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
		fetches.Add(1)
		nntp.FireAcquireHook(ctx)
		time.Sleep(100 * time.Millisecond) // under hedgeMin
		return []byte("data"), nil
	})
	time.Sleep(20 * time.Millisecond)
	d, _ := c.Do("k", nntp.Live, func(context.Context) ([]byte, error) {
		fetches.Add(1)
		return []byte("dup"), nil
	})
	if string(d) != "data" || fetches.Load() != 1 || c.Hedges.Load() != 0 {
		t.Fatalf("got %q after %d fetches, %d hedges", d, fetches.Load(), c.Hedges.Load())
	}
}
