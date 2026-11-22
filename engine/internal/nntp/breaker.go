package nntp

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// ErrProviderDown means a provider is skipped for now: its login was
// refused (an expired account, a wrong password) or connecting to it kept
// failing (host down, wrong port, a blackholed address). Like any failure
// other than "no such article" it is never proof an article is missing.
var ErrProviderDown = errors.New("nntp: provider unavailable")

// Breaker timings (vars for tests). After a refused login a provider is
// left alone for authMin, doubling per failed probe up to authMax: an auth
// service with a short outage heals by itself, bad credentials cost one
// login attempt per backoff instead of one per article. Connection
// failures open the breaker after connFailures in a row, for connMin
// doubling up to connMax.
var (
	authMin      = 30 * time.Second
	authMax      = 10 * time.Minute
	connMin      = 2 * time.Second
	connMax      = time.Minute
	connFailures = 3
)

// breaker guards dialing for one pool (Ether's usenet breaker). Connections
// already open keep working while it is open: only new dials are refused.
// Once the backoff ends one dial is let through as a probe; its result
// closes the breaker or opens it again for twice as long. Guarded by
// pool.mu.
type breaker struct {
	until   time.Time     // dials refused before this
	backoff time.Duration // last backoff (doubles per failed probe)
	streak  int           // connection failures in a row
	auth    bool          // opened by a refused login
	err     error         // what opened it
	probing bool          // a probe dial is running
}

// open reports whether dials are refused now (a probe in flight counts).
func (b *breaker) open(now time.Time) bool {
	return !b.until.IsZero() && (now.Before(b.until) || b.probing)
}

// admit decides whether one dial may go ahead. probe is true when it is
// the single recovery attempt after a backoff.
func (b *breaker) admit(now time.Time) (probe bool, err error) {
	if b.until.IsZero() {
		return false, nil
	}
	if now.Before(b.until) || b.probing {
		return false, b.downErr(now)
	}
	b.probing = true
	return true, nil
}

func (b *breaker) downErr(now time.Time) error {
	kind := "connection failing"
	if b.auth {
		kind = "login refused"
	}
	wait := max(b.until.Sub(now), 0).Round(time.Second)
	return fmt.Errorf("%w (%s, retry in %s): %v", ErrProviderDown, kind, wait, b.err)
}

// result records a dial's outcome. It returns a non-zero backoff when the
// breaker opened (or stayed open), and recovered=true when a dial worked
// after the breaker had been open.
func (b *breaker) result(now time.Time, probe bool, ctxErr error, err error) (opened time.Duration, recovered bool) {
	if probe {
		b.probing = false
	}
	switch {
	case err == nil:
		recovered = !b.until.IsZero()
		*b = breaker{}
		return 0, recovered
	case ctxErr != nil:
		// The caller gave up; that says nothing about the server.
		return 0, false
	case errors.Is(err, ErrBusy):
		// The server answered: it is up and the login path works far
		// enough. "Too many connections" has its own throttle.
		if probe {
			*b = breaker{}
			return 0, true
		}
		b.streak = 0
		return 0, false
	case errors.Is(err, ErrAuth):
		b.auth = true
		b.backoff = doubled(b.backoff, authMin, authMax, probe || b.until.IsZero())
	default:
		b.streak++
		if !probe && b.until.IsZero() && b.streak < connFailures {
			return 0, false
		}
		// Dials that started before the breaker opened and fail after it
		// keep the backoff; only the first opening and failed probes grow it.
		b.backoff = doubled(b.backoff, connMin, connMax, probe || b.until.IsZero())
		b.auth = false
	}
	b.err = err
	b.until = now.Add(b.backoff)
	return b.backoff, false
}

// doubled is the next backoff: lo at first, then twice the last, at most hi.
// grow=false keeps the current one (concurrent failures racing the first).
func doubled(d, lo, hi time.Duration, grow bool) time.Duration {
	switch {
	case d <= 0:
		return lo
	case !grow:
		return d
	}
	return min(max(2*d, lo), hi)
}

// dialGuarded dials through the breaker without touching slots.
func (pl *pool) dialGuarded(ctx context.Context) (*conn, error) {
	pl.mu.Lock()
	probe, err := pl.brk.admit(time.Now())
	pl.mu.Unlock()
	if err != nil {
		return nil, err
	}
	c, err := dial(ctx, pl.p)
	pl.mu.Lock()
	opened, recovered := pl.brk.result(time.Now(), probe, ctx.Err(), err)
	auth := pl.brk.auth
	pl.mu.Unlock()
	if pl.c != nil {
		switch {
		case opened > 0:
			kind := "connection failing"
			if auth {
				kind = "login refused"
			}
			pl.c.problem(pl.p.Name, "provider paused", Prefetch,
				fmt.Errorf("%s; not dialed again for %s: %w", kind, opened.Round(time.Second), err))
		case recovered:
			pl.c.info("usenet provider back", "provider", pl.p.Name)
		}
	}
	return c, err
}

// down reports whether the breaker is refusing dials and no idle
// connection is left to use, so asking this provider now can only fail.
func (pl *pool) down() (bool, error) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	now := time.Now()
	if !pl.brk.open(now) || len(pl.idle) > 0 {
		return false, nil
	}
	return true, pl.brk.downErr(now)
}

// state is the breaker's state for status pages: "ok", "auth_failed" or
// "down", and how long until the next try.
func (pl *pool) state() (string, time.Duration) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	now := time.Now()
	if !pl.brk.open(now) {
		return "ok", 0
	}
	if pl.brk.auth {
		return "auth_failed", max(pl.brk.until.Sub(now), 0)
	}
	return "down", max(pl.brk.until.Sub(now), 0)
}
