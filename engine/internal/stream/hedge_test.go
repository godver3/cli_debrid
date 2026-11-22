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

// When the slow background fetch finishes first after all, the hedge's own
// download is cancelled instead of fetching the article a second time.
func TestHedgeCancelledWhenSlowFetchWins(t *testing.T) {
	old := hedgeMin
	hedgeMin = 50 * time.Millisecond
	defer func() { hedgeMin = old }()
	c := NewCache(1 << 20)
	slowDone := make(chan struct{})
	go c.Do("k", nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
		nntp.FireAcquireHook(ctx)
		<-slowDone
		return []byte("slow"), nil
	})
	time.Sleep(20 * time.Millisecond)
	hedgeCancelled := make(chan struct{})
	d, err := c.Do("k", nntp.Live, func(ctx context.Context) ([]byte, error) {
		close(slowDone) // the slow fetch answers while the hedge is on the wire
		<-ctx.Done()
		close(hedgeCancelled)
		return nil, ctx.Err()
	})
	if err != nil || string(d) != "slow" {
		t.Fatalf("got %q %v, want the slow fetch's data", d, err)
	}
	select {
	case <-hedgeCancelled:
	case <-time.After(time.Second):
		t.Fatal("hedge download kept running after the slow fetch won")
	}
}
