package nntp

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"reflect"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Client fetches articles across providers: primaries first, then backups.
type Client struct {
	pools []*pool
	mode  atomic.Int32  // LoadMode
	rr    atomic.Uint64 // Balanced: rotates the start among equal loads

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

// info logs a provider event at info level (when a logger is set).
func (c *Client) info(msg string, args ...any) {
	c.logMu.Lock()
	log := c.log
	c.logMu.Unlock()
	if log != nil {
		log.Info(msg, args...)
	}
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
// are always tried before any backup. A client with no providers is
// allowed (a debrid-only mount): every fetch fails inconclusively.
func NewClient(providers []Provider) (*Client, error) {
	ps := append([]Provider(nil), providers...)
	sort.SliceStable(ps, func(i, j int) bool { return !ps[i].Backup && ps[j].Backup })
	c := &Client{}
	for _, p := range ps {
		pl := newPool(p)
		pl.c = c
		c.pools = append(c.pools, pl)
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
	if len(c.pools) == 0 {
		// Never proof: with no provider nothing was asked.
		return &InconclusiveError{ID: id, Last: ErrNoProviders}
	}
	notFound, mismatched := 0, 0
	var last error
	hint := missHintFrom(ctx)
	left := make([]*pool, 0, len(c.pools))
	for _, pl := range c.pools {
		// A provider whose breaker is open (refused login, unreachable)
		// is skipped without a dial: before, every article paid for a
		// connect, TLS and login (or the whole dial timeout) on it.
		if isDown, err := pl.down(); isDown {
			last = fmt.Errorf("%s: %w", pl.p.Name, err)
			continue
		}
		left = append(left, pl)
	}
	for len(left) > 0 {
		k, pre, preErr, got := c.pick(ctx, left, prio)
		if !got {
			// Every server of this tier is busy: take whichever frees a
			// connection first (cli_mount races them), not only the first.
			k, pre, preErr = acquireAny(ctx, left, tier(left), prio)
			if preErr != nil && ctx.Err() != nil {
				return ctx.Err()
			}
			got = true
		}
		pl := left[k]
		left = append(left[:k:k], left[k+1:]...)
		answered, gotNotFound, gotMismatch, got430 := false, false, false, false
		// One retry per provider: an idle connection the server already
		// closed fails on first use, which says nothing about the article.
		for try := 0; try < 2 && !answered; try++ {
			var cn *conn
			var err error
			if try == 0 && got {
				cn, err = pre, preErr
			} else {
				cn, err = pl.acquire(ctx, prio)
			}
			if err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
				if errors.Is(err, ErrBusy) {
					pl.throttle(err)
					c.problem(pl.p.Name, "too many connections", prio, err)
				} else if errors.Is(err, ErrProviderDown) {
					// paused: logged once when its breaker opened
				} else {
					c.problem(pl.p.Name, "connect", prio, err)
				}
				break
			}
			acquired(ctx)
			idleFor := time.Since(cn.lastUsed)
			began := time.Now()
			found, err := fn(cn)
			pl.record(began, cn.lastBytes, found, err)
			switch {
			case err == nil && found:
				pl.release(cn)
				if hint != nil {
					hint.hit(pl.p.Name)
				}
				return nil
			case errors.Is(err, errWrongArticle):
				// This server's copy isn't the one the file needs (a
				// Message-ID collision or a re-post): ask the next server.
				pl.release(cn)
				gotMismatch = true
				answered = true
			case err == nil || IsNotFound(err):
				pl.release(cn)
				gotNotFound = true
				answered = true
				got430 = IsNotFound(err)
			case errors.Is(err, ErrBusy):
				pl.discard(cn)
				pl.throttle(err)
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
				c.problem(pl.p.Name, "too many connections", prio, err)
				answered = true
			case errors.Is(err, ErrAuth):
				pl.discard(cn)
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
				c.problem(pl.p.Name, "login", prio, err)
				answered = true // retrying the same credentials won't help
			default:
				// "400" on a connection in use is the server closing it (idle
				// timeout): a stale connection like a broken pipe, retried on
				// a fresh one below. Any other status is a clean answer.
				var pe *ProtocolError
				if errors.As(err, &pe) && !isClosing(err) {
					pl.release(cn) // a clean status answer; the connection is fine
					answered = true
				} else {
					pl.discard(cn)
					if try > 0 { // the first failure is often just a stale idle connection
						c.problem(pl.p.Name, "connection", prio, err)
					} else if idleFor >= staleIdleAge {
						// The server closed it while idle, and the other
						// connections idle as long with it: drop them so the
						// retry gets a live (or freshly dialed) connection
						// instead of failing the same way ("broken pipe").
						pl.dropIdleOlderThan(min(idleFor, staleIdleCap))
					}
				}
				last = fmt.Errorf("%s: %w", pl.p.Name, err)
			}
		}
		if gotNotFound {
			notFound++
			if hint != nil {
				hint.miss(pl.p.Name)
			}
		}
		if got430 && pl.p.backbone() != "" {
			// Same backbone, same article store: its other servers would
			// answer the same, so they count as asked.
			keep := left[:0]
			for _, o := range left {
				if o.p.backbone() == pl.p.backbone() {
					notFound++
					continue
				}
				keep = append(keep, o)
			}
			left = keep
		}
		if gotMismatch {
			mismatched++
		}
	}
	// Proof needs a "no such article" from every provider; a provider that
	// failed any other way might have had it.
	if notFound == len(c.pools) {
		c.missing.Add(1)
		return ErrArticleNotFound
	}
	if mismatched > 0 && notFound+mismatched == len(c.pools) {
		return fmt.Errorf("%w: %s", ErrMismatch, id)
	}
	if last == nil {
		last = errors.New("no provider answered")
	}
	return &InconclusiveError{ID: id, Last: last}
}

// pick chooses the provider to ask next among those not asked yet. In
// FillFirst mode the first primary in order with a free connection is
// used, so a busy first server spills over to the next instead of
// queueing; in Balanced mode the least-loaded primary is tried first (see
// Client.order). When every primary is full the caller waits on all of
// them (acquireAny). Backups are asked only once no primary is left. When
// got is true, pre/preErr is the connection already taken from left[k].
func (c *Client) pick(ctx context.Context, left []*pool, prio Priority) (k int, pre *conn, preErr error, got bool) {
	idx := c.order(ctx, left, tier(left), prio)
	for _, i := range idx {
		if cn, ok, err := left[i].tryAcquire(ctx, prio); ok {
			return i, cn, err, true
		}
	}
	return idx[0], nil, nil, false
}

// tier: indexes into left of the servers to ask now: the primaries, or the
// backups once no primary is left. Never empty for a non-empty left.
func tier(left []*pool) []int {
	backups := true
	for _, pl := range left {
		if !pl.p.Backup {
			backups = false
			break
		}
	}
	var idx []int
	for i, pl := range left {
		if pl.p.Backup == backups {
			idx = append(idx, i)
		}
	}
	return idx
}

// acquireAny waits on every server in idx at once and returns the first
// connection (or dial slot, then dialed) handed out; the other waits are
// withdrawn and any slot they got meanwhile is passed on.
func acquireAny(ctx context.Context, left []*pool, idx []int, prio Priority) (k int, c *conn, err error) {
	chans := make([]chan *slot, len(idx))
	won := -1
	defer func() {
		for i, ch := range chans {
			if ch != nil && i != won {
				left[idx[i]].leave(prio, ch)
			}
		}
	}()
	for i, j := range idx {
		c, dial, ok, ch := left[j].enqueue(prio)
		if ok {
			won = i
			if dial {
				c, err = left[j].dial(ctx)
			}
			return j, c, err
		}
		chans[i] = ch
	}
	cases := make([]reflect.SelectCase, 0, len(idx)+1)
	for _, ch := range chans {
		cases = append(cases, reflect.SelectCase{Dir: reflect.SelectRecv, Chan: reflect.ValueOf(ch)})
	}
	cases = append(cases, reflect.SelectCase{Dir: reflect.SelectRecv, Chan: reflect.ValueOf(ctx.Done())})
	chosen, v, _ := reflect.Select(cases)
	if chosen == len(idx) {
		return idx[0], nil, ctx.Err()
	}
	won = chosen
	j := idx[chosen]
	if s := v.Interface().(*slot); s.c != nil {
		return j, s.c, nil
	}
	c, err = left[j].dial(ctx)
	return j, c, err
}

// A reused connection that fails after sitting idle at least staleIdleAge
// is taken as closed by the server; idle connections unused for
// min(its idle time, staleIdleCap) are dropped with it.
const (
	staleIdleAge = 5 * time.Second
	staleIdleCap = 30 * time.Second
)

// errWrongArticle: the accept check rejected one server's copy.
var errWrongArticle = errors.New("nntp: wrong article on this server")

// Body downloads and decodes one article.
func (c *Client) Body(ctx context.Context, prio Priority, id string) ([]byte, Meta, error) {
	return c.BodyAccept(ctx, prio, id, nil)
}

// BodyAccept is Body with a check of the decoded article: when accept
// returns false the next server is asked, as for a missing article. If no
// server has an accepted copy and at least one had a rejected one, the
// error is ErrMismatch (not ErrArticleNotFound).
func (c *Client) BodyAccept(ctx context.Context, prio Priority, id string, accept func([]byte, Meta) bool) ([]byte, Meta, error) {
	var data []byte
	var meta Meta
	err := c.attempt(ctx, prio, id, func(cn *conn) (bool, error) {
		d, m, err := cn.body(id)
		if errors.Is(err, ErrTruncated) {
			return false, nil // as below
		}
		if err != nil {
			return false, err
		}
		if m.PartSize > 0 && int64(len(d)) < m.PartSize {
			// The article's own =ypart says it holds more than it decoded
			// to: this server's copy is truncated. Like a missing article,
			// the next server is asked; short or missing on every server is
			// proof (cli_mount's relayoutDead: "a segment that decodes short").
			return false, nil
		}
		if accept != nil && !accept(d, m) {
			return false, errWrongArticle
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
	// State is "ok", "auth_failed" (login refused) or "down" (connecting
	// keeps failing); RetryIn is the seconds until the next try.
	State   string  `json:"state"`
	RetryIn float64 `json:"retry_in_s,omitempty"`
	// ServerLimit is a connection limit the server stated in a refusal
	// (0 = none learned); CapNow the limit in force right now.
	ServerLimit int `json:"server_limit,omitempty"`
	CapNow      int `json:"cap_now"`
}

// Warm tops every provider up to its MinIdle connections.
func (c *Client) Warm(ctx context.Context) {
	for _, pl := range c.pools {
		go func() {
			// Wrong login, host or port shows up here first, before any read.
			if err := pl.warm(ctx); err != nil && ctx.Err() == nil {
				c.problem(pl.p.Name, "warm", Prefetch, err)
			}
		}()
	}
}

// Stats returns per-provider connection use plus lifetime counters.
func (c *Client) Stats() (pools []PoolStats, bytes, articles, missing int64) {
	for _, pl := range c.pools {
		open, idle, waiting := pl.stats()
		lastErr, _ := pl.lastErr.Load().(string)
		state, retry := pl.state()
		learned, capNow := pl.limits()
		pools = append(pools, PoolStats{State: state, RetryIn: retry.Seconds(), ServerLimit: learned, CapNow: capNow, Name: pl.p.Name, Backup: pl.p.Backup, Max: pl.p.MaxConns,
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
