package nntp

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

func TestParseLoadMode(t *testing.T) {
	for in, want := range map[string]LoadMode{"": FillFirst, "fill": FillFirst, "Balanced": Balanced, " balanced ": Balanced, "junk": FillFirst} {
		if got := ParseLoadMode(in); got != want {
			t.Errorf("ParseLoadMode(%q) = %v, want %v", in, got, want)
		}
	}
}

// Fill first (the default): an idle client always asks the first primary.
// Balanced: idle primaries take turns, and the backup is never asked.
func TestBalancedSpreadsOverPrimaries(t *testing.T) {
	arts := nntptest.Post("bal", "bal.bin", awkward(500), 500)
	for _, mode := range []LoadMode{FillFirst, Balanced} {
		t.Run(mode.String(), func(t *testing.T) {
			a := nntptest.Start(t, arts...)
			b := nntptest.Start(t, arts...)
			c3 := nntptest.Start(t, arts...)
			bk := nntptest.Start(t, arts...)
			bp := provider(bk, "backup", 4)
			bp.Backup = true
			c, _ := NewClient([]Provider{provider(a, "a", 4), provider(b, "b", 4), provider(c3, "c", 4), bp})
			c.SetLoadMode(mode)
			defer c.Close()
			for range 30 {
				if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
					t.Fatal(err)
				}
			}
			na, nb, nc := a.Bodies.Load(), b.Bodies.Load(), c3.Bodies.Load()
			if bk.Bodies.Load() != 0 {
				t.Fatalf("backup asked %d times", bk.Bodies.Load())
			}
			if mode == FillFirst && (na != 30 || nb != 0 || nc != 0) {
				t.Fatalf("fill: a=%d b=%d c=%d, want 30/0/0", na, nb, nc)
			}
			if mode == Balanced && (na < 8 || nb < 8 || nc < 8) {
				t.Fatalf("balanced: a=%d b=%d c=%d, want each about 10", na, nb, nc)
			}
		})
	}
}

// Balanced never reorders backups: once the primary has no copy, the
// first backup in list order is asked every time (the second is not given
// a share by load).
func TestBalancedKeepsBackupOrder(t *testing.T) {
	arts := nntptest.Post("bko", "bko.bin", awkward(500), 500)
	a := nntptest.Start(t) // the primary lacks it
	b1 := nntptest.Start(t, arts...)
	b2 := nntptest.Start(t, arts...)
	p1, p2 := provider(b1, "b1", 4), provider(b2, "b2", 4)
	p1.Backup, p2.Backup = true, true
	c, _ := NewClient([]Provider{provider(a, "a", 4), p1, p2})
	c.SetLoadMode(Balanced)
	defer c.Close()
	for range 20 {
		if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
			t.Fatal(err)
		}
	}
	if n1, n2 := b1.Bodies.Load(), b2.Bodies.Load(); n1 != 20 || n2 != 0 {
		t.Fatalf("backups b1=%d b2=%d, want 20/0", n1, n2)
	}
}

// Under concurrent load each primary carries work in proportion to its
// connections: the bigger account is never left idle while a small one is
// the only one in use.
func TestBalancedUsesBothUnderLoad(t *testing.T) {
	arts := nntptest.Post("load", "load.bin", awkward(500), 500)
	a := nntptest.Start(t, arts...)
	b := nntptest.Start(t, arts...)
	a.SetDelay(30 * time.Millisecond)
	b.SetDelay(30 * time.Millisecond)
	c, _ := NewClient([]Provider{provider(a, "a", 2), provider(b, "b", 6)})
	c.SetLoadMode(Balanced)
	defer c.Close()
	var wg sync.WaitGroup
	for range 40 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	na, nb := a.Bodies.Load(), b.Bodies.Load()
	if na+nb != 40 || nb <= na {
		t.Fatalf("a(2 conns)=%d b(6 conns)=%d: the bigger account should carry more", na, nb)
	}
}

// Balanced changes who is asked first, never what counts as proof: an
// article missing everywhere is still asked on every provider (backups
// included) and only then reported missing.
func TestBalancedProofStillAsksEveryProvider(t *testing.T) {
	a, b := nntptest.Start(t), nntptest.Start(t)
	bk := nntptest.Start(t)
	bp := provider(bk, "backup", 1)
	bp.Backup = true
	c, _ := NewClient([]Provider{provider(a, "a", 2), provider(b, "b", 2), bp})
	c.SetLoadMode(Balanced)
	defer c.Close()
	for i := range 4 {
		id := fmt.Sprintf("gone%d@test", i)
		if _, _, err := c.Body(context.Background(), Live, id); !IsNotFound(err) {
			t.Fatalf("got %v, want proof", err)
		}
	}
	if a.Asked() != 4 || b.Asked() != 4 || bk.Asked() != 4 {
		t.Fatalf("asked a=%d b=%d backup=%d, want 4 each", a.Asked(), b.Asked(), bk.Asked())
	}

	// Missing on both primaries + a flaky backup: inconclusive, not proof.
	bk.Drop("flaky@test")
	_, _, err := c.Body(context.Background(), Live, "flaky@test")
	var inc *InconclusiveError
	if IsNotFound(err) || !errors.As(err, &inc) {
		t.Fatalf("got %v, want inconclusive", err)
	}
}

// A primary that keeps lacking one file's articles is asked last for that
// file (balanced only), but still asked when the others lack one too.
func TestMissHintAsksShortRetentionServerLast(t *testing.T) {
	arts := nntptest.Post("old", "old.bin", awkward(5000), 250) // 20 segments
	short := nntptest.Start(t)                                  // retention too short: has none
	full := nntptest.Start(t, arts...)
	c, _ := NewClient([]Provider{provider(short, "short", 4), provider(full, "full", 4)})
	c.SetLoadMode(Balanced)
	defer c.Close()
	hint := NewMissHint()
	ctx := WithMissHint(context.Background(), hint)
	for _, a := range arts {
		if _, _, err := c.Body(ctx, Live, a.ID); err != nil {
			t.Fatal(err)
		}
	}
	if n := short.Asked(); n > missDemote {
		t.Fatalf("short-retention server asked %d times for one file, want at most %d", n, missDemote)
	}
	// Missing on full as well: the demoted server is still asked (proof).
	before := short.Asked()
	if _, _, err := c.Body(ctx, Live, "nowhere@test"); !IsNotFound(err) {
		t.Fatalf("got %v, want proof", err)
	}
	if short.Asked() != before+1 {
		t.Fatal("demoted server was skipped instead of asked last")
	}
}

// "502 Too many connections" is not a login failure: the next provider is
// asked, the full one stops dialing for a while, and it's not proof.
func TestTooManyConnectionsFailsOverAndThrottles(t *testing.T) {
	arts := nntptest.Post("busy", "busy.bin", awkward(500), 500)
	full := nntptest.Start(t, arts...)
	full.Refuse("502 Too many connections")
	ok := nntptest.Start(t, arts...)
	c, _ := NewClient([]Provider{provider(full, "full", 4), provider(ok, "ok", 4)})
	defer c.Close()
	if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
		t.Fatalf("failover: %v", err)
	}
	accepts := full.Accepts.Load()
	for range 5 {
		if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
			t.Fatal(err)
		}
	}
	if full.Accepts.Load() != accepts {
		t.Fatalf("throttled server dialed %d more times", full.Accepts.Load()-accepts)
	}

	// Only that server: inconclusive wrapping ErrBusy, not auth, not proof.
	c2, _ := NewClient([]Provider{provider(full, "full", 1)})
	defer c2.Close()
	_, _, err := c2.Body(context.Background(), Live, arts[0].ID)
	if IsNotFound(err) || !errors.Is(err, ErrBusy) || errors.Is(err, ErrAuth) {
		t.Fatalf("got %v, want inconclusive ErrBusy", err)
	}
}

func TestClassifyBusy(t *testing.T) {
	for _, tc := range []struct {
		code          int
		msg           string
		busy, connect bool // classify, classifyConnect
	}{
		{502, "Too many connections", true, true},
		{481, "Max connections reached for this account", true, true},
		// A bare 400 is busy only at greeting/login; on a connection in use
		// it's the server closing it.
		{400, "Service temporarily unavailable", false, true},
		{400, "Idle timeout, closing connection", false, true},
		{502, "Permission denied", false, false},
		{481, "Authentication failed", false, false},
		{430, "no such article (too many)", false, false},
	} {
		if err := classify(tc.code, tc.msg); errors.Is(err, ErrBusy) != tc.busy {
			t.Errorf("classify(%d %q) = %v, busy want %v", tc.code, tc.msg, err, tc.busy)
		}
		if err := classifyConnect(tc.code, tc.msg); errors.Is(err, ErrBusy) != tc.connect {
			t.Errorf("classifyConnect(%d %q) = %v, busy want %v", tc.code, tc.msg, err, tc.connect)
		}
	}
	if !isClosing(classify(400, "Idle timeout")) || isClosing(classify(500, "what?")) {
		t.Error("isClosing wrong")
	}
}

// Connections the server ended while idle answer "400 Idle timeout": that's
// a stale connection, retried on a fresh one. It must not throttle the
// server, skip it, or shrink its connections (first play after a quiet
// night with a single provider).
func TestIdleTimeout400IsStaleNotBusy(t *testing.T) {
	arts := nntptest.Post("idle", "idle.bin", awkward(500), 500)
	s := nntptest.Start(t, arts...)
	s.SetDelay(50 * time.Millisecond)
	c, _ := NewClient([]Provider{provider(s, "only", 4)})
	defer c.Close()
	var wg sync.WaitGroup
	for range 4 { // open all four connections
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	pl := c.pools[0]
	pl.mu.Lock()
	if len(pl.idle) != 4 {
		t.Fatalf("idle = %d, want 4", len(pl.idle))
	}
	for _, cn := range pl.idle { // idle long enough to count as stale
		cn.lastUsed = time.Now().Add(-time.Minute)
	}
	pl.mu.Unlock()
	s.SetDelay(0)
	s.IdleTimeout()

	for i := range 5 {
		if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
			t.Fatalf("read %d after idle timeout: %v", i, err)
		}
	}
	if pl.throttled() {
		t.Fatal("idle-timeout 400 throttled the server")
	}
	pl.mu.Lock()
	limit := pl.maxConnsLocked()
	pl.mu.Unlock()
	if limit != 4 {
		t.Fatalf("connection limit %d after idle timeout, want 4", limit)
	}
}

// While throttled, the playback reserve shrinks with the lowered limit:
// background work can't take every connection that is left.
func TestThrottleKeepsReserve(t *testing.T) {
	pl := newPool(Provider{Name: "p", MaxConns: 20, Reserve: 4})
	pl.capConns, pl.capUntil = 6, time.Now().Add(time.Minute)
	pl.mu.Lock()
	defer pl.mu.Unlock()
	if !pl.prefetchMayTake(1) {
		t.Fatal("background blocked below the throttled limit minus reserve")
	}
	if pl.prefetchMayTake(2) {
		t.Fatal("background may take a throttled pool's reserve (limit 6, reserve 4)")
	}
	pl.capConns = 1 // reserve never takes the last connection
	if !pl.prefetchMayTake(0) {
		t.Fatal("throttled to one connection: background locked out entirely")
	}
}

// Back-to-back refusals each give a slot back; re-capping at the shrinking
// open count would walk the pool down to one connection.
func TestThrottleNeverLowersCap(t *testing.T) {
	pl := newPool(Provider{Name: "p", MaxConns: 20})
	pl.open = 8
	pl.throttle(nil)
	pl.open = 3
	pl.throttle(nil)
	pl.mu.Lock()
	got := pl.maxConnsLocked()
	pl.mu.Unlock()
	if got != 8 {
		t.Fatalf("cap = %d after a second refusal, want 8 (kept)", got)
	}
	pl.capUntil = time.Now().Add(-time.Second) // expired: a new refusal re-caps
	pl.throttle(nil)
	pl.mu.Lock()
	got = pl.maxConnsLocked()
	pl.mu.Unlock()
	if got != 3 {
		t.Fatalf("cap = %d after expiry, want 3", got)
	}
}

// A dial slot freed while throttled (a refused dial, a dropped connection)
// isn't handed to a queued waiter, which would only dial into the same
// refusal; the last open slot still is, so the queue can't be stranded.
func TestThrottledDropDoesNotChainDials(t *testing.T) {
	pl := newPool(Provider{Name: "p", MaxConns: 20})
	pl.open = 3
	pl.capConns, pl.capUntil = 3, time.Now().Add(time.Minute)
	ch := make(chan *slot, 1)
	pl.queues[Live] = []chan *slot{ch}
	pl.drop()
	select {
	case <-ch:
		t.Fatal("throttled pool handed a dial slot to a waiter")
	default:
	}
	if pl.open != 2 {
		t.Fatalf("open = %d, want 2", pl.open)
	}
	pl.open = 1 // the only slot left: it must reach the waiter
	pl.drop()
	select {
	case s := <-ch:
		if s.c != nil {
			t.Fatal("want an empty dial slot")
		}
	default:
		t.Fatal("last slot not passed on: waiter stranded")
	}
	// A released connection still goes to the waiter while throttled.
	pl.open = 3
	pl.queues[Live] = []chan *slot{ch}
	pl.release(&conn{})
	select {
	case s := <-ch:
		if s.c == nil {
			t.Fatal("released connection not handed over")
		}
	default:
		t.Fatal("released connection kept from a waiter while throttled")
	}
}
