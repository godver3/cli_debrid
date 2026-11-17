package nntp

import (
	"context"
	"sync"
	"time"
)

// Priority orders waiters for a connection. Live reads (a player waiting on
// bytes) always get the next free connection before any prefetch.
type Priority int

const (
	Prefetch Priority = iota
	Live
)

// pool hands out up to MaxConns connections to one provider.
type pool struct {
	p      Provider
	mu     sync.Mutex
	idle   []*conn
	open   int
	queues [2][]chan *slot // indexed by Priority
	closed bool
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

// acquire returns a ready connection, dialing if allowed.
func (pl *pool) acquire(ctx context.Context, prio Priority) (*conn, error) {
	pl.mu.Lock()
	if pl.closed {
		pl.mu.Unlock()
		return nil, context.Canceled
	}
	if n := len(pl.idle); n > 0 {
		c := pl.idle[n-1]
		pl.idle = pl.idle[:n-1]
		pl.mu.Unlock()
		return c, nil
	}
	if pl.open < pl.p.MaxConns {
		pl.open++
		pl.mu.Unlock()
		return pl.dial(ctx)
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
	for _, c := range pl.idle {
		if maxIdle > 0 && time.Since(c.lastUsed) < maxIdle {
			keep = append(keep, c)
			continue
		}
		c.close()
		pl.open--
	}
	pl.idle = keep
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
	return pl.open, len(pl.idle), len(pl.queues[Live]) + len(pl.queues[Prefetch])
}
