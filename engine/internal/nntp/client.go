package nntp

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Client fetches articles across providers: primaries first, then backups.
type Client struct {
	pools []*pool

	bytes    atomic.Int64
	articles atomic.Int64
	missing  atomic.Int64
}

// NewClient builds a client. Primary providers keep their given order and
// are always tried before any backup.
func NewClient(providers []Provider) (*Client, error) {
	if len(providers) == 0 {
		return nil, ErrNoProviders
	}
	ps := append([]Provider(nil), providers...)
	sort.SliceStable(ps, func(i, j int) bool { return !ps[i].Backup && ps[j].Backup })
	c := &Client{}
	for _, p := range ps {
		c.pools = append(c.pools, newPool(p))
	}
	return c, nil
}

type acquireHookKey struct{}

// WithAcquireHook returns a context whose requests call fn once, the first
// time a connection is obtained (the request has left the queue and is on
// the wire). Callers use it to tell a queued request from a running one.
func WithAcquireHook(ctx context.Context, fn func()) context.Context {
	var once sync.Once
	return context.WithValue(ctx, acquireHookKey{}, func() { once.Do(fn) })
}

func acquired(ctx context.Context) {
	if fn, ok := ctx.Value(acquireHookKey{}).(func()); ok {
		fn()
	}
}

// InconclusiveError means no provider proved the article missing, but none
// delivered it either (timeouts, dropped connections, auth failures...).
type InconclusiveError struct {
	ID   string
	Last error
}

func (e *InconclusiveError) Error() string {
	return fmt.Sprintf("nntp: %s not fetched (inconclusive): %v", e.ID, e.Last)
}

func (e *InconclusiveError) Unwrap() error { return e.Last }

// attempt runs fn on a connection from each provider in turn until one
// succeeds. fn returns done=true when the provider gave a definite answer.
//
// The result is ErrArticleNotFound only when every provider answered
// "no such article"; if any provider failed for another reason the answer
// is an InconclusiveError, because that provider might have had it.
func (c *Client) attempt(ctx context.Context, prio Priority, id string,
	fn func(*conn) (found bool, err error)) error {
	notFound := 0
	var last error
	for _, pl := range c.pools {
		answered, gotNotFound := false, false
		// One retry per provider: an idle connection the server already
		// closed fails on first use, which says nothing about the article.
		for try := 0; try < 2 && !answered; try++ {
			cn, err := pl.acquire(ctx, prio)
			if err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
				break
			}
			acquired(ctx)
			found, err := fn(cn)
			switch {
			case err == nil && found:
				pl.release(cn)
				return nil
			case err == nil || IsNotFound(err):
				pl.release(cn)
				gotNotFound = true
				answered = true
			case errors.Is(err, ErrAuth):
				pl.discard(cn)
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
				answered = true // retrying the same credentials won't help
			default:
				var pe *ProtocolError
				if errors.As(err, &pe) {
					pl.release(cn) // a clean status answer; the connection is fine
					answered = true
				} else {
					pl.discard(cn)
				}
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
			}
		}
		if gotNotFound {
			notFound++
		}
	}
	// Proof needs a "no such article" from every provider; a provider that
	// failed any other way might have had it.
	if notFound == len(c.pools) {
		c.missing.Add(1)
		return ErrArticleNotFound
	}
	if last == nil {
		last = errors.New("no provider answered")
	}
	return &InconclusiveError{ID: id, Last: last}
}

// Body downloads and decodes one article.
func (c *Client) Body(ctx context.Context, prio Priority, id string) ([]byte, Meta, error) {
	var data []byte
	var meta Meta
	err := c.attempt(ctx, prio, id, func(cn *conn) (bool, error) {
		d, m, err := cn.body(id)
		if err != nil {
			return false, err
		}
		data, meta = d, m
		return true, nil
	})
	if err != nil {
		return nil, Meta{}, err
	}
	c.bytes.Add(int64(len(data)))
	c.articles.Add(1)
	return data, meta, nil
}

// Stat reports whether an article exists. (false, nil) is proof it is
// missing on every provider; an error means the answer is unknown.
func (c *Client) Stat(ctx context.Context, prio Priority, id string) (bool, error) {
	err := c.attempt(ctx, prio, id, func(cn *conn) (bool, error) {
		return cn.stat(id)
	})
	if IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}

// PoolStats is a snapshot of one provider's connections.
type PoolStats struct {
	Name    string
	Backup  bool
	Max     int
	Open    int
	Idle    int
	Waiting int
}

// Stats returns per-provider connection use plus lifetime counters.
func (c *Client) Stats() (pools []PoolStats, bytes, articles, missing int64) {
	for _, pl := range c.pools {
		open, idle, waiting := pl.stats()
		pools = append(pools, PoolStats{Name: pl.p.Name, Backup: pl.p.Backup, Max: pl.p.MaxConns,
			Open: open, Idle: idle, Waiting: waiting})
	}
	return pools, c.bytes.Load(), c.articles.Load(), c.missing.Load()
}

// CloseIdle drops connections unused for longer than maxIdle.
func (c *Client) CloseIdle(maxIdle time.Duration) {
	for _, pl := range c.pools {
		pl.closeIdle(maxIdle)
	}
}

// Close shuts every pool down.
func (c *Client) Close() {
	for _, pl := range c.pools {
		pl.shutdown()
	}
}

// FireAcquireHook runs ctx's acquire hook, if any. For tests and for
// fetchers that obtain data without going through Client.
func FireAcquireHook(ctx context.Context) { acquired(ctx) }
