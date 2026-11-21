package nntp

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// Priority orders waiters for a connection. Live reads (a player waiting on
// bytes) always get the next free connection before any prefetch. Read is
// a mount read Plex has not confirmed as playback (scanner, thumbnails,
// ffprobe) while Plex playback protection is on: it goes ahead of prefetch
// but, like prefetch, may not use the playback reserve.
type Priority int

const (
	Prefetch Priority = iota
	Read
	Live
)

// pool hands out up to MaxConns connections to one provider.
type pool struct {
	p       Provider
	mu      sync.Mutex
	idle    []*conn
	open    int
	queues  [3][]chan *slot // indexed by Priority
	closed  bool
	warming int // dials in progress for MinIdle

	// Per-provider counters for status pages.
	articles, bytes, missing, errors atomic.Int64
	latencyNs                        atomic.Int64 // moving average per fetch
	lastErr                          atomic.Value // string
}

// reserve is the number of connections only live reads may use.
func (pl *pool) reserve() int {
	return min(max(pl.p.Reserve, 0), pl.p.MaxConns-1)
}

// prefetchMayTake says whether non-live work may take one more connection
// when busy connections are already handed out (lock held).
func (pl *pool) prefetchMayTake(busy int) bool {
	return busy < pl.p.MaxConns-pl.reserve()
}

// record updates the counters after one fetch.
func (pl *pool) record(start time.Time, n int, found bool, err error) {
	switch {
	case err == nil && found:
		pl.articles.Add(1)
		pl.bytes.Add(int64(n))
	case err == nil || IsNotFound(err):
		pl.missing.Add(1)
	default:
		pl.errors.Add(1)
		pl.lastErr.Store(err.Error())
	}
	d := time.Since(start).Nanoseconds()
	if old := pl.latencyNs.Load(); old == 0 {
		pl.latencyNs.Store(d)
	} else {
		pl.latencyNs.Store(old + (d-old)/8)
	}
}

// slot is what a waiter receives: either an idle connection or permission
// to dial a new one (conn == nil).
type slot struct{ c *conn }

func newPool(p Provider) *pool {
	if p.MaxConns <= 0 {
		p.MaxConns = 1
	}
	return &pool{p: p}
}

// tryAcquire is acquire without waiting: ok is false when the pool has no
// connection free for prio right now.
func (pl *pool) tryAcquire(ctx context.Context, prio Priority) (c *conn, ok bool, err error) {
	pl.mu.Lock()
	c, dial, ok := pl.takeLocked(prio)
	pl.mu.Unlock()
	if !ok {
		return nil, false, nil
	}
	if dial {
		c, err = pl.dial(ctx)
	}
	return c, true, err
}

// takeLocked takes an idle connection, or a slot to dial (dial true), when
// one is free for prio. pl.mu is held.
func (pl *pool) takeLocked(prio Priority) (c *conn, dial, ok bool) {
	if pl.closed {
		return nil, false, false
	}
	busy := pl.open - len(pl.idle)
	if prio != Live && !pl.prefetchMayTake(busy) {
		return nil, false, false
	}
	if n := len(pl.idle); n > 0 {
		c = pl.idle[n-1]
		pl.idle = pl.idle[:n-1]
		return c, false, true
	}
	if pl.open < pl.p.MaxConns {
		pl.open++
		return nil, true, true
	}
	return nil, false, false
}

// acquire returns a ready connection, dialing if allowed, waiting when the
// pool is full.
func (pl *pool) acquire(ctx context.Context, prio Priority) (*conn, error) {
	pl.mu.Lock()
	if pl.closed {
		pl.mu.Unlock()
		return nil, context.Canceled
	}
	if c, dial, ok := pl.takeLocked(prio); ok {
		pl.mu.Unlock()
		if dial {
			return pl.dial(ctx)
		}
		return c, nil
	}
	ch := make(chan *slot, 1)
	pl.queues[prio] = append(pl.queues[prio], ch)
	pl.mu.Unlock()

	select {
	case s := <-ch:
		if s.c != nil {
			return s.c, nil
		}
		return pl.dial(ctx)
	case <-ctx.Done():
		pl.mu.Lock()
		pl.removeWaiter(prio, ch)
		pl.mu.Unlock()
		// A slot may have been handed over between ctx firing and the lock.
		select {
		case s := <-ch:
			pl.passOn(s)
		default:
		}
		return nil, ctx.Err()
	}
}

// dial opens a connection for a slot this caller already owns.
func (pl *pool) dial(ctx context.Context) (*conn, error) {
	c, err := dial(ctx, pl.p)
	if err != nil {
		pl.drop()
		return nil, err
	}
	return c, nil
}

// release returns a healthy connection for reuse.
func (pl *pool) release(c *conn) { pl.passOn(&slot{c: c}) }

// discard closes a broken connection and frees its slot.
func (pl *pool) discard(c *conn) {
	c.close()
	pl.drop()
}

// drop frees one slot: the next waiter may dial in its place.
func (pl *pool) drop() { pl.passOn(&slot{}) }

// passOn gives a slot to the highest-priority waiter, or parks it.
func (pl *pool) passOn(s *slot) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	for prio := Live; prio >= Prefetch; prio-- {
		if q := pl.queues[prio]; len(q) > 0 {
			// The slot being passed is still counted busy; a non-live
			// waiter may only keep it outside the playback reserve.
			if prio != Live && !pl.prefetchMayTake(pl.open-len(pl.idle)-1) {
				break
			}
			ch := q[0]
			pl.queues[prio] = q[1:]
			ch <- s
			return
		}
	}
	if s.c == nil {
		pl.open--
		return
	}
	if pl.closed {
		s.c.close()
		pl.open--
		return
	}
	s.c.lastUsed = time.Now()
	pl.idle = append(pl.idle, s.c)
}

// enqueue takes a free connection (or a slot to dial) when there is one;
// otherwise it registers a waiter and returns its channel. ch is nil when
// ok is true or the pool is closed.
func (pl *pool) enqueue(prio Priority) (c *conn, dial, ok bool, ch chan *slot) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	if pl.closed {
		return nil, false, false, nil
	}
	if c, dial, ok := pl.takeLocked(prio); ok {
		return c, dial, true, nil
	}
	ch = make(chan *slot, 1)
	pl.queues[prio] = append(pl.queues[prio], ch)
	return nil, false, false, ch
}

// leave withdraws a waiter; a slot already handed to it goes to the next.
func (pl *pool) leave(prio Priority, ch chan *slot) {
	pl.mu.Lock()
	pl.removeWaiter(prio, ch)
	pl.mu.Unlock()
	select {
	case s := <-ch:
		pl.passOn(s)
	default:
	}
}

func (pl *pool) removeWaiter(prio Priority, ch chan *slot) {
	q := pl.queues[prio]
	for i, w := range q {
		if w == ch {
			pl.queues[prio] = append(q[:i:i], q[i+1:]...)
			return
		}
	}
}

// closeIdle drops connections idle longer than maxIdle (all when 0).
func (pl *pool) closeIdle(maxIdle time.Duration) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	keep := pl.idle[:0]
	floor := 0
	if maxIdle > 0 {
		floor = min(pl.p.MinIdle, pl.p.MaxConns) // warm connections stay
	}
	for _, c := range pl.idle {
		if (maxIdle > 0 && time.Since(c.lastUsed) < maxIdle) || len(keep) < floor {
			keep = append(keep, c)
			continue
		}
		c.close()
		pl.open--
	}
	pl.idle = keep
}

// dropIdleOlderThan closes idle connections unused for at least age. A
// provider that closes idle connections closes all of them at once, so when
// one turns out dead, the others idle as long are dead too.
func (pl *pool) dropIdleOlderThan(age time.Duration) int {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	keep := pl.idle[:0]
	dropped := 0
	for _, c := range pl.idle {
		if time.Since(c.lastUsed) < age {
			keep = append(keep, c)
			continue
		}
		c.close()
		pl.open--
		dropped++
	}
	pl.idle = keep
	return dropped
}

// warm dials connections until MinIdle are open (idle or busy), so a new
// session doesn't pay for the handshake. A failure stops this round (the
// next warm or read tries again) and is returned for the caller to report.
func (pl *pool) warm(ctx context.Context) error {
	want := min(pl.p.MinIdle, pl.p.MaxConns)
	for {
		pl.mu.Lock()
		if pl.closed || pl.open+pl.warming >= want || pl.open >= pl.p.MaxConns {
			pl.mu.Unlock()
			return nil
		}
		pl.open++
		pl.warming++
		pl.mu.Unlock()
		c, err := dial(ctx, pl.p)
		pl.mu.Lock()
		pl.warming--
		pl.mu.Unlock()
		if err != nil {
			pl.drop()
			pl.errors.Add(1)
			pl.lastErr.Store(err.Error())
			return err
		}
		pl.release(c)
	}
}

func (pl *pool) shutdown() {
	pl.mu.Lock()
	pl.closed = true
	pl.mu.Unlock()
	pl.closeIdle(0)
}

// stats returns (open, idle, waiting) for status pages.
func (pl *pool) stats() (int, int, int) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	return pl.open, len(pl.idle), len(pl.queues[Live]) + len(pl.queues[Read]) + len(pl.queues[Prefetch])
}
