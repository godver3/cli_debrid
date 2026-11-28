package stream

import (
	"context"
	"errors"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// AcceptFetcher is a Fetcher that can check each server's copy of an
// article and ask the next server when it is the wrong one. *nntp.Client
// implements it; test fakes may not (then a wrong copy fails the read).
type AcceptFetcher interface {
	BodyAccept(ctx context.Context, prio nntp.Priority, id string, accept func([]byte, nntp.Meta) bool) ([]byte, nntp.Meta, error)
}

// Read retries. A playback read that fails for a reason that proves
// nothing (timeouts, dropped connections, "502 too many connections" on
// every server) is tried again with backoff instead of failing the read
// straight away, like cli_mount's retried failover. Prefetches don't
// retry: a later read-ahead or the live read asks again.
var (
	retryBudget   = 45 * time.Second
	retryFirst    = 500 * time.Millisecond
	retryMaxDelay = 8 * time.Second
)

// fetchBody fetches one article for a file read: wrong copies are skipped
// server by server (when f can check them), and unproven failures of
// non-prefetch reads are retried until retryBudget runs out or ctx ends.
func fetchBody(ctx context.Context, f Fetcher, prio nntp.Priority, id string,
	accept func([]byte, nntp.Meta) bool) ([]byte, nntp.Meta, error) {
	start := time.Now()
	delay := retryFirst
	for {
		if err := ctx.Err(); err != nil {
			return nil, nntp.Meta{}, err
		}
		var data []byte
		var meta nntp.Meta
		var err error
		if af, ok := f.(AcceptFetcher); ok && accept != nil {
			data, meta, err = af.BodyAccept(ctx, prio, id, accept)
		} else {
			data, meta, err = f.Body(ctx, prio, id)
		}
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, nntp.Meta{}, ctxErr
		}
		if err == nil || !retryable(prio, err) || time.Since(start)+delay > retryBudget {
			return data, meta, err
		}
		t := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			t.Stop()
			return nil, nntp.Meta{}, ctx.Err()
		case <-t.C:
		}
		delay = min(2*delay, retryMaxDelay)
	}
}

// retryable: only unproven failures of reads someone is waiting for.
func retryable(prio nntp.Priority, err error) bool {
	if prio == nntp.Prefetch {
		return false
	}
	if nntp.IsNotFound(err) || errors.Is(err, nntp.ErrMismatch) || errors.Is(err, nntp.ErrNoProviders) {
		return false
	}
	var inc *nntp.InconclusiveError
	return errors.As(err, &inc) && !errors.Is(inc.Last, nntp.ErrNoProviders)
}
