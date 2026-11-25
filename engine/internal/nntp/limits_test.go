package nntp

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

func TestStatedLimit(t *testing.T) {
	for msg, want := range map[string]int{
		"nntp: too many connections: 502 Too many connections":    0,
		"nntp: too many connections: 481 max connections reached": 0,
		"481 Max connections exceeded (ip 10.0.0.1)":              0,
		"502 connection limit (8) reached":                        8,
		"482 Max 8 connections":                                   8,
		"502 maximum number of connections (12) reached":          12,
		"502 you have 25 connections open, limit is 20":           20,
		"481 only 10 simultaneous connections allowed":            10,
		"400 Service temporarily unavailable":                     0,
		"502 Connection limit: 30":                                30,
	} {
		if got := statedLimit(msg); got != want {
			t.Errorf("statedLimit(%q) = %d, want %d", msg, got, want)
		}
	}
}

// A stated limit lowers the pool for its life (minus ~10%); it never rises.
func TestThrottleLearnsStatedLimit(t *testing.T) {
	pl := newPool(Provider{Name: "p", MaxConns: 50})
	pl.open = 30
	pl.throttle(fmt.Errorf("%w: 502 connection limit (20) reached", ErrBusy))
	pl.capUntil = time.Now().Add(-time.Hour) // hold and widening long over
	pl.mu.Lock()
	got := pl.maxConnsLocked()
	pl.mu.Unlock()
	if got != 18 {
		t.Fatalf("limit = %d, want 18 (20 minus 10%%)", got)
	}
	pl.throttle(fmt.Errorf("%w: 502 connection limit (40) reached", ErrBusy))
	if l, _ := pl.limits(); l != 18 {
		t.Fatalf("learned = %d after a higher stated limit, want 18 kept", l)
	}
}

// After the hold the cap widens one connection per widenEvery up to MaxConns.
func TestThrottleWidensGradually(t *testing.T) {
	pl := newPool(Provider{Name: "p", MaxConns: 10})
	pl.open = 4
	pl.throttle(errors.New("busy"))
	pl.mu.Lock()
	defer pl.mu.Unlock()
	if got := pl.maxConnsLocked(); got != 4 {
		t.Fatalf("during hold = %d, want 4", got)
	}
	pl.capUntil = time.Now().Add(-2*widenEvery - time.Millisecond)
	if got := pl.maxConnsLocked(); got != 7 {
		t.Fatalf("two steps into widening = %d, want 7", got)
	}
	if pl.holdingLocked() {
		t.Fatal("still holding while widening")
	}
	pl.capUntil = time.Now().Add(-time.Hour)
	if got := pl.maxConnsLocked(); got != 10 || pl.capConns != 0 {
		t.Fatalf("after widening = %d (cap %d), want 10 and cleared", got, pl.capConns)
	}
}
