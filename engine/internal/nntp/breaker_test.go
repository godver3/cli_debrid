package nntp

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

func withBreakerTimes(t *testing.T, auth, conn time.Duration) {
	t.Helper()
	oa, oam, oc, ocm := authMin, authMax, connMin, connMax
	authMin, authMax, connMin, connMax = auth, 4*auth, conn, 4*conn
	t.Cleanup(func() { authMin, authMax, connMin, connMax = oa, oam, oc, ocm })
}

// A refused login pauses the provider: later articles go straight to the
// next one instead of logging in (and failing) again for each of them.
func TestRefusedLoginPausesProvider(t *testing.T) {
	withBreakerTimes(t, time.Hour, time.Hour)
	arts := nntptest.Post("f", "file.bin", awkward(50_000), 10_000)
	bad := nntptest.Start(t, arts...)
	bad.SetAuth("u", "right")
	good := nntptest.Start(t, arts...)
	pb := provider(bad, "expired", 4)
	pb.Username, pb.Password = "u", "wrong"
	c, _ := NewClient([]Provider{pb, provider(good, "good", 4)})
	defer c.Close()
	for _, a := range arts {
		if _, _, err := c.Body(context.Background(), Live, a.ID); err != nil {
			t.Fatal(err)
		}
	}
	if n := bad.Accepts.Load(); n != 1 {
		t.Fatalf("expired provider dialed %d times for %d articles, want 1", n, len(arts))
	}
	pools, _, _, _ := c.Stats()
	if pools[0].State != "auth_failed" || pools[0].RetryIn <= 0 {
		t.Fatalf("state %q retry %.0fs, want auth_failed with a retry time", pools[0].State, pools[0].RetryIn)
	}
}

// A provider that keeps failing to connect is paused after connFailures,
// then probed once the backoff ends, and used again when it works.
func TestUnreachableProviderPausedThenProbed(t *testing.T) {
	withBreakerTimes(t, time.Hour, 150*time.Millisecond)
	arts := nntptest.Post("f", "file.bin", awkward(100_000), 10_000)
	flaky := nntptest.Start(t, arts...)
	flaky.Refuse("500 broken")
	good := nntptest.Start(t, arts...)
	c, _ := NewClient([]Provider{provider(flaky, "flaky", 1), provider(good, "good", 4)})
	defer c.Close()
	for _, a := range arts {
		if _, _, err := c.Body(context.Background(), Live, a.ID); err != nil {
			t.Fatal(err)
		}
	}
	if n := flaky.Accepts.Load(); n != int64(connFailures) {
		t.Fatalf("failing provider dialed %d times, want %d before pausing", n, connFailures)
	}
	flaky.Refuse("")
	time.Sleep(200 * time.Millisecond)
	before := flaky.Bodies.Load()
	if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
		t.Fatal(err)
	}
	if flaky.Bodies.Load() != before+1 {
		t.Fatal("recovered provider not used after its backoff")
	}
	pools, _, _, _ := c.Stats()
	if pools[0].State != "ok" {
		t.Fatalf("state %q after a working probe, want ok", pools[0].State)
	}
}

// With every provider paused the read fails at once, inconclusively.
func TestAllPausedIsInconclusiveAndFast(t *testing.T) {
	withBreakerTimes(t, time.Hour, time.Hour)
	arts := nntptest.Post("f", "file.bin", awkward(10_000), 10_000)
	s := nntptest.Start(t, arts...)
	s.SetAuth("u", "right")
	p := provider(s, "only", 2)
	p.Username, p.Password = "u", "wrong"
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	c.Body(context.Background(), Live, arts[0].ID)
	start := time.Now()
	_, _, err := c.Body(context.Background(), Live, arts[0].ID)
	var inc *InconclusiveError
	if !errors.As(err, &inc) || !errors.Is(err, ErrProviderDown) || IsNotFound(err) {
		t.Fatalf("err = %v, want inconclusive provider-down", err)
	}
	if time.Since(start) > 100*time.Millisecond || s.Accepts.Load() != 1 {
		t.Fatalf("paused provider still dialed (%d accepts, %s)", s.Accepts.Load(), time.Since(start))
	}
}

func TestBreakerResult(t *testing.T) {
	var b breaker
	now := time.Now()
	for i := 1; i < connFailures; i++ {
		if d, _ := b.result(now, false, nil, errors.New("refused")); d != 0 {
			t.Fatalf("opened after %d failures", i)
		}
	}
	if d, _ := b.result(now, false, nil, errors.New("refused")); d != connMin {
		t.Fatalf("backoff %s, want %s", d, connMin)
	}
	// A dial racing the opening keeps the backoff.
	if d, _ := b.result(now, false, nil, errors.New("refused")); d != connMin {
		t.Fatalf("racing failure grew backoff to %s", d)
	}
	probe, err := b.admit(now.Add(connMin))
	if !probe || err != nil {
		t.Fatalf("no probe after backoff: %v", err)
	}
	if _, err := b.admit(now.Add(connMin)); !errors.Is(err, ErrProviderDown) {
		t.Fatal("second dial let through while probing")
	}
	if d, _ := b.result(now, true, nil, errors.New("refused")); d != 2*connMin {
		t.Fatalf("failed probe backoff %s, want doubled", d)
	}
	if _, rec := b.result(now, false, nil, nil); !rec || b.open(now) {
		t.Fatal("success did not close the breaker")
	}
	// A caller giving up says nothing.
	if d, _ := b.result(now, false, context.Canceled, context.Canceled); d != 0 || b.streak != 0 {
		t.Fatal("cancelled dial counted as a failure")
	}
}
