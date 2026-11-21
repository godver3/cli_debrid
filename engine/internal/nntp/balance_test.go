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
		code int
		msg  string
		busy bool
	}{
		{502, "Too many connections", true},
		{481, "Max connections reached for this account", true},
		{400, "Service temporarily unavailable", true},
		{502, "Permission denied", false},
		{481, "Authentication failed", false},
		{430, "no such article (too many)", false},
	} {
		err := classify(tc.code, tc.msg)
		if errors.Is(err, ErrBusy) != tc.busy {
			t.Errorf("classify(%d %q) = %v, busy want %v", tc.code, tc.msg, err, tc.busy)
		}
	}
}
