package stream

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"testing/synctest"
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

func TestJoinedFetchHonorsCancellation(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		cache := NewCache(1 << 20)
		const key = "remote:joined" // lower-priority fetches for this key are never hedged
		release := make(chan struct{})
		go cache.Do(key, nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
			nntp.FireAcquireHook(ctx)
			<-release
			return nil, errors.New("background fetch failed")
		})
		synctest.Wait()
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		done := make(chan error, 1)
		var retries atomic.Int32
		go func() {
			_, err := cache.DoCtx(ctx, key, nntp.Read, func(context.Context) ([]byte, error) {
				retries.Add(1)
				return nil, errors.New("unexpected retry")
			})
			done <- err
		}()
		synctest.Wait()
		cancel()
		synctest.Wait()
		close(release)
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("joined read error = %v, want cancellation", err)
			}
		default:
			t.Error("joined read kept waiting for the background fetch after cancellation")
		}
		synctest.Wait()
		if got := retries.Load(); got != 0 {
			t.Errorf("cancelled read started %d retries", got)
		}
	})
}

func TestHedgeHonorsCancellationAfterBackgroundFetchFails(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		cache := NewCache(1 << 20)
		oldRelease := make(chan struct{})
		go cache.Do("k", nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
			nntp.FireAcquireHook(ctx)
			<-oldRelease
			return nil, errors.New("background fetch failed")
		})
		synctest.Wait()

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		hedgeStarted, hedgeRelease := make(chan struct{}), make(chan struct{})
		defer close(hedgeRelease)
		done := make(chan error, 1)
		go func() {
			_, err := cache.DoCtx(ctx, "k", nntp.Read, func(context.Context) ([]byte, error) {
				close(hedgeStarted)
				<-hedgeRelease // simulate a fetch stuck on the wire
				return nil, errors.New("hedge failed")
			})
			done <- err
		}()
		<-hedgeStarted
		close(oldRelease)
		synctest.Wait()
		cancel()
		synctest.Wait()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("hedged read error = %v, want cancellation", err)
			}
		default:
			t.Fatal("hedged read kept waiting after cancellation")
		}
	})
}
