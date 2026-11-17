package nntp

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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

	log      *slog.Logger
	logMu    sync.Mutex
	lastLogs map[string]*logState // provider+kind -> throttle state
}

type logState struct {
	at         time.Time
	suppressed int
}

// problemEvery: the same provider problem is logged at most this often;
// repeats in between are counted and reported with the next line.
const problemEvery = 30 * time.Second

// SetLogger turns on logging of provider problems (connect, login,
// connection errors). Article-level answers are not logged.
func (c *Client) SetLogger(log *slog.Logger) {
	c.logMu.Lock()
	c.log = log
	c.lastLogs = map[string]*logState{}
	c.logMu.Unlock()
}

func (c *Client) problem(provider, kind string, prio Priority, err error) {
	c.logMu.Lock()
	log := c.log
	if log == nil {
		c.logMu.Unlock()
		return
	}
	key := provider + "\x00" + kind
	st := c.lastLogs[key]
	if st == nil {
		st = &logState{}
		c.lastLogs[key] = st
	}
	if !st.at.IsZero() && time.Since(st.at) < problemEvery {
		st.suppressed++
		c.logMu.Unlock()
		return
	}
	repeats := st.suppressed
	st.at, st.suppressed = time.Now(), 0
	c.logMu.Unlock()
	log.Warn("usenet provider problem", "provider", provider, "kind", kind, "err", err,
		"class", prioName(prio), "repeats_since_last_log", repeats)
}

func prioName(p Priority) string {
	switch p {
	case Live:
		return "playback"
	case Read:
		return "mount read"
	}
	return "background"
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
				c.problem(pl.p.Name, "connect", prio, err)
				break
			}
			acquired(ctx)
			began := time.Now()
			found, err := fn(cn)
			pl.record(began, cn.lastBytes, found, err)
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
				c.problem(pl.p.Name, "login", prio, err)
				answered = true // retrying the same credentials won't help
			default:
				var pe *ProtocolError
				if errors.As(err, &pe) {
					pl.release(cn) // a clean status answer; the connection is fine
					answered = true
				} else {
					pl.discard(cn)
					if try > 0 { // the first failure is often just a stale idle connection
						c.problem(pl.p.Name, "connection", prio, err)
					}
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

// PoolStats is a snapshot of one provider's connections and counters.
type PoolStats struct {
	Name      string  `json:"name"`
	Backup    bool    `json:"backup"`
	Max       int     `json:"max"`
	Open      int     `json:"open"`
	Idle      int     `json:"idle"`
	Waiting   int     `json:"waiting"`
	Reserve   int     `json:"reserve"`
	Articles  int64   `json:"articles"`
	Bytes     int64   `json:"bytes"`
	Missing   int64   `json:"missing"`
	Errors    int64   `json:"errors"`
	LatencyMs float64 `json:"latency_ms"`
	LastError string  `json:"last_error,omitempty"`
}

// Warm tops every provider up to its MinIdle connections.
func (c *Client) Warm(ctx context.Context) {
	for _, pl := range c.pools {
		go pl.warm(ctx)
	}
}

// Stats returns per-provider connection use plus lifetime counters.
func (c *Client) Stats() (pools []PoolStats, bytes, articles, missing int64) {
	for _, pl := range c.pools {
		open, idle, waiting := pl.stats()
		lastErr, _ := pl.lastErr.Load().(string)
		pools = append(pools, PoolStats{Name: pl.p.Name, Backup: pl.p.Backup, Max: pl.p.MaxConns,
			Reserve: pl.reserve(), Articles: pl.articles.Load(), Bytes: pl.bytes.Load(), Missing: pl.missing.Load(),
			Errors: pl.errors.Load(), LatencyMs: float64(pl.latencyNs.Load()) / 1e6, LastError: lastErr,
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
