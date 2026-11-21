package stream

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// flaky fails the first n fetches inconclusively, then delivers.
type flaky struct {
	fails int32
	calls atomic.Int32
	err   error
}

func (f *flaky) Body(ctx context.Context, prio nntp.Priority, id string) ([]byte, nntp.Meta, error) {
	if f.calls.Add(1) <= f.fails {
		if f.err != nil {
			return nil, nntp.Meta{}, f.err
		}
		return nil, nntp.Meta{}, &nntp.InconclusiveError{ID: id, Last: errors.New("502 too many connections")}
	}
	return []byte("data"), nntp.Meta{}, nil
}

func fastRetries(t *testing.T) {
	old1, old2 := retryFirst, retryBudget
	retryFirst, retryBudget = time.Millisecond, time.Second
	t.Cleanup(func() { retryFirst, retryBudget = old1, old2 })
}

// A playback read that every server fails for an unproven reason is tried
// again instead of failing the read (cli_mount retries with backoff).
func TestLiveReadRetriesInconclusive(t *testing.T) {
	fastRetries(t)
	f := &flaky{fails: 3}
	d, _, err := fetchBody(context.Background(), f, nntp.Live, "a", nil)
	if err != nil || string(d) != "data" || f.calls.Load() != 4 {
		t.Fatalf("got %q, %v after %d calls", d, err, f.calls.Load())
	}
}

// Prefetches, proof and a used-up budget are not retried.
func TestNoRetryForPrefetchProofOrBudget(t *testing.T) {
	fastRetries(t)
	f := &flaky{fails: 3}
	if _, _, err := fetchBody(context.Background(), f, nntp.Prefetch, "a", nil); err == nil || f.calls.Load() != 1 {
		t.Fatalf("prefetch: %v after %d calls", err, f.calls.Load())
	}
	f = &flaky{fails: 3, err: nntp.ErrArticleNotFound}
	if _, _, err := fetchBody(context.Background(), f, nntp.Live, "a", nil); !nntp.IsNotFound(err) || f.calls.Load() != 1 {
		t.Fatalf("proof: %v after %d calls", err, f.calls.Load())
	}
	retryBudget = 20 * time.Millisecond
	retryFirst = 15 * time.Millisecond
	f = &flaky{fails: 100}
	if _, _, err := fetchBody(context.Background(), f, nntp.Live, "a", nil); err == nil || f.calls.Load() > 3 {
		t.Fatalf("budget: %v after %d calls", err, f.calls.Load())
	}
}
