package nntp

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"math/rand"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/yenc"
)

func provider(s *nntptest.Server, name string, conns int) Provider {
	host, port := s.Addr()
	return Provider{Name: name, Host: host, Port: port, MaxConns: conns, Timeout: 5 * time.Second}
}

// awkward returns data that exercises every yEnc escape and NNTP dot-stuffing
// case: bytes encoding to NUL/CR/LF/'=', and lines that start with '.'.
func awkward(n int) []byte {
	r := rand.New(rand.NewSource(1))
	b := make([]byte, n)
	r.Read(b)
	for i := 0; i < n; i += 97 {
		b[i] = '.' - 42 // encodes to '.'
	}
	for i := 5; i < n; i += 131 {
		b[i] = 256 - 42 // encodes to NUL
	}
	return b
}

func TestBodyRoundTripBothDecoders(t *testing.T) {
	data := awkward(300_000)
	arts := nntptest.Post("f", "file.bin", data, 100_000)
	for _, pure := range []bool{false, true} {
		t.Run(map[bool]string{false: "default", true: "pure-go"}[pure], func(t *testing.T) {
			old := yenc.UsePureGo
			yenc.UsePureGo = pure
			defer func() { yenc.UsePureGo = old }()

			s := nntptest.Start(t, arts...)
			c, err := NewClient([]Provider{provider(s, "a", 1)})
			if err != nil {
				t.Fatal(err)
			}
			defer c.Close()
			var got []byte
			for i, a := range arts {
				d, m, err := c.Body(context.Background(), Live, a.ID)
				if err != nil {
					t.Fatalf("part %d: %v", i+1, err)
				}
				if m.FileSize != int64(len(data)) || m.Offset != int64(i*100_000) || m.PartSize != int64(len(d)) {
					t.Fatalf("part %d meta %+v", i+1, m)
				}
				got = append(got, d...)
			}
			if !bytes.Equal(got, data) {
				t.Fatal("decoded bytes differ from posted data")
			}
			// One connection served every article: the terminator was consumed.
			if n := s.Accepts.Load(); n != 1 {
				t.Fatalf("connections opened = %d, want 1", n)
			}
		})
	}
}

func TestFailoverAndProof(t *testing.T) {
	arts := nntptest.Post("x", "x.bin", awkward(1000), 1000)
	a := nntptest.Start(t)          // has nothing
	b := nntptest.Start(t, arts...) // has the article
	c, _ := NewClient([]Provider{provider(a, "a", 2), provider(b, "b", 2)})
	defer c.Close()

	if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
		t.Fatalf("failover to b: %v", err)
	}
	_, _, err := c.Body(context.Background(), Live, "nope@test")
	if !IsNotFound(err) {
		t.Fatalf("missing everywhere: got %v, want ErrArticleNotFound", err)
	}
	ok, err := c.Stat(context.Background(), Live, "nope@test")
	if ok || err != nil {
		t.Fatalf("Stat missing = %v, %v; want false, nil", ok, err)
	}
	ok, err = c.Stat(context.Background(), Live, arts[0].ID)
	if !ok || err != nil {
		t.Fatalf("Stat present = %v, %v", ok, err)
	}
}

func TestMissingPlusFlakyIsInconclusive(t *testing.T) {
	a := nntptest.Start(t) // says 430
	b := nntptest.Start(t) // drops the connection
	b.Drop("gone@test")
	c, _ := NewClient([]Provider{provider(a, "a", 1), provider(b, "b", 1)})
	defer c.Close()
	_, _, err := c.Body(context.Background(), Live, "gone@test")
	var inc *InconclusiveError
	if IsNotFound(err) || !errors.As(err, &inc) {
		t.Fatalf("got %v; a flaky provider must make the answer inconclusive, not proof", err)
	}
}

func TestBackupsOnlyAfterPrimaries(t *testing.T) {
	arts := nntptest.Post("y", "y.bin", awkward(500), 500)
	primary := nntptest.Start(t, arts...)
	backup := nntptest.Start(t, arts...)
	bp := provider(backup, "backup", 1)
	bp.Backup = true
	c, _ := NewClient([]Provider{bp, provider(primary, "primary", 1)}) // backup listed first on purpose
	defer c.Close()
	if _, _, err := c.Body(context.Background(), Live, arts[0].ID); err != nil {
		t.Fatal(err)
	}
	if backup.Bodies.Load() != 0 || primary.Bodies.Load() != 1 {
		t.Fatalf("backup bodies=%d primary=%d", backup.Bodies.Load(), primary.Bodies.Load())
	}
}

func TestBadCredentialsAreNotProof(t *testing.T) {
	s := nntptest.Start(t)
	s.SetAuth("u", "right")
	p := provider(s, "a", 1)
	p.Username, p.Password = "u", "wrong"
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	_, _, err := c.Body(context.Background(), Live, "any@test")
	if IsNotFound(err) || !errors.Is(err, ErrAuth) {
		t.Fatalf("got %v, want inconclusive wrapping ErrAuth", err)
	}
}

func TestLiveOvertakesQueuedPrefetch(t *testing.T) {
	var arts []nntptest.Article
	for _, id := range []string{"hold", "p1", "p2", "live"} {
		arts = append(arts, nntptest.Post(id, id, awkward(100), 100)...)
	}
	s := nntptest.Start(t, arts...)
	s.SetDelay(150 * time.Millisecond)
	c, _ := NewClient([]Provider{provider(s, "a", 1)}) // one connection: everyone queues
	defer c.Close()

	var wg sync.WaitGroup
	fetch := func(id string, prio Priority) {
		defer wg.Done()
		if _, _, err := c.Body(context.Background(), prio, id+".1@test"); err != nil {
			t.Errorf("%s: %v", id, err)
		}
	}
	wg.Add(4)
	go fetch("hold", Prefetch)
	time.Sleep(50 * time.Millisecond) // "hold" owns the connection
	go fetch("p1", Prefetch)
	go fetch("p2", Prefetch)
	time.Sleep(50 * time.Millisecond) // both prefetches are queued
	go fetch("live", Live)
	wg.Wait()

	order := s.BodyOrder()
	if len(order) != 4 || order[0] != "hold.1@test" || order[1] != "live.1@test" {
		t.Fatalf("BODY order %v: the live read must be served right after the busy connection frees", order)
	}
}

// With 3 connections and 1 reserved, prefetch work holds at most 2: a live
// read gets the third straight away instead of queueing behind it.
func TestPlaybackReserve(t *testing.T) {
	var arts []nntptest.Article
	for _, id := range []string{"p1", "p2", "p3", "p4", "live"} {
		arts = append(arts, nntptest.Post(id, id, awkward(100), 100)...)
	}
	s := nntptest.Start(t, arts...)
	s.SetDelay(300 * time.Millisecond)
	p := provider(s, "a", 3)
	p.Reserve = 1
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	var wg sync.WaitGroup
	for _, id := range []string{"p1", "p2", "p3", "p4"} {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			c.Body(context.Background(), Prefetch, id+".1@test")
		}(id)
	}
	time.Sleep(100 * time.Millisecond)
	if pools, _, _, _ := c.Stats(); pools[0].Open-pools[0].Idle != 2 || pools[0].Reserve != 1 {
		t.Fatalf("prefetch should hold exactly 2 of 3 connections: %+v", pools[0])
	}
	start := time.Now()
	if _, _, err := c.Body(context.Background(), Live, "live.1@test"); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(start); took > 450*time.Millisecond {
		t.Fatalf("live read waited %v: it should use the reserved connection at once", took)
	}
	wg.Wait() // the queued prefetches still all complete
	pools, _, _, _ := c.Stats()
	if pools[0].Articles != 5 || pools[0].Bytes != 500 || pools[0].LatencyMs <= 0 {
		t.Fatalf("counters: %+v", pools[0])
	}
}

// An unconfirmed mount read (Read) goes ahead of queued prefetches but,
// like them, can't use the playback reserve.
func TestReadClassOutsideReserveAheadOfPrefetch(t *testing.T) {
	var arts []nntptest.Article
	for _, id := range []string{"hold", "p1", "read", "live"} {
		arts = append(arts, nntptest.Post(id, id, awkward(100), 100)...)
	}
	s := nntptest.Start(t, arts...)
	s.SetDelay(200 * time.Millisecond)
	p := provider(s, "a", 2)
	p.Reserve = 1
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	var wg sync.WaitGroup
	fetch := func(id string, prio Priority) {
		defer wg.Done()
		if _, _, err := c.Body(context.Background(), prio, id+".1@test"); err != nil {
			t.Errorf("%s: %v", id, err)
		}
	}
	wg.Add(3)
	go fetch("hold", Prefetch) // takes the one non-reserved connection
	time.Sleep(50 * time.Millisecond)
	go fetch("p1", Prefetch)
	time.Sleep(30 * time.Millisecond)
	go fetch("read", Read) // queued too: the free connection is the reserve
	time.Sleep(50 * time.Millisecond)
	if pools, _, _, _ := c.Stats(); pools[0].Open-pools[0].Idle != 1 {
		t.Fatalf("Read must not take the reserved connection: %+v", pools[0])
	}
	start := time.Now()
	wg.Add(1)
	fetch("live", Live)
	if took := time.Since(start); took > 350*time.Millisecond {
		t.Fatalf("live read waited %v for the reserve", took)
	}
	wg.Wait()
	order := s.BodyOrder()
	ri, pi := -1, -1
	for i, id := range order {
		switch id {
		case "read.1@test":
			ri = i
		case "p1.1@test":
			pi = i
		}
	}
	if ri < 0 || pi < 0 || ri > pi {
		t.Fatalf("BODY order %v: the Read must be served before the queued prefetch", order)
	}
}

func TestReserveNeverStarvesASingleConnection(t *testing.T) {
	s := nntptest.Start(t, nntptest.Post("x", "x", awkward(100), 100)...)
	p := provider(s, "a", 1)
	p.Reserve = 5 // clamped to MaxConns-1 = 0
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, _, err := c.Body(ctx, Prefetch, "x.1@test"); err != nil {
		t.Fatalf("prefetch on a 1-connection provider: %v", err)
	}
}

func TestWarmKeepsMinIdleOpen(t *testing.T) {
	s := nntptest.Start(t, nntptest.Post("x", "x", awkward(100), 100)...)
	p := provider(s, "a", 4)
	p.MinIdle = 2
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	c.Warm(context.Background())
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if pools, _, _, _ := c.Stats(); pools[0].Idle == 2 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	c.CloseIdle(time.Nanosecond) // the idle reaper keeps the warm floor
	if pools, _, _, _ := c.Stats(); pools[0].Idle != 2 || pools[0].Open != 2 {
		t.Fatalf("warm connections: %+v", pools[0])
	}
	if pools, _, _, _ := c.Stats(); pools[0].Articles != 0 {
		t.Fatal("warming must not fetch anything")
	}
}

// A wrong login shows up at warm-up: counted, logged and in Stats, so the
// UI's health check can say why nothing connects.
func TestWarmReportsLoginFailure(t *testing.T) {
	s := nntptest.Start(t)
	s.SetAuth("u", "right")
	p := provider(s, "a", 4)
	p.MinIdle, p.Username, p.Password = 2, "u", "wrong"
	c, _ := NewClient([]Provider{p})
	defer c.Close()
	var buf syncBuf
	c.SetLogger(slog.New(slog.NewTextHandler(&buf, nil)))
	c.Warm(context.Background())
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !strings.Contains(buf.String(), "kind=warm") {
		time.Sleep(10 * time.Millisecond)
	}
	pools, _, _, _ := c.Stats()
	if pools[0].Open != 0 || pools[0].Errors == 0 || pools[0].LastError == "" {
		t.Fatalf("stats after failed warm: %+v", pools[0])
	}
	if !strings.Contains(buf.String(), "usenet provider problem") {
		t.Fatalf("not logged: %q", buf.String())
	}
}

type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) { s.mu.Lock(); defer s.mu.Unlock(); return s.b.Write(p) }
func (s *syncBuf) String() string              { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

// A debrid-only engine has no news server: fetches must never be "proof"
// that an article is missing.
func TestNoProvidersIsNeverProof(t *testing.T) {
	c, err := NewClient(nil)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = c.Body(context.Background(), Live, "x@y")
	if err == nil || IsNotFound(err) || !errors.Is(err, ErrNoProviders) {
		t.Fatalf("want inconclusive ErrNoProviders, got %v", err)
	}
}

// A provider that closes idle connections closes all of them. The first
// stale one tells us the rest are dead too: the retry must not pick another
// stale one (that failed, logged "broken pipe" and moved to the next server).
func TestStaleIdleConnectionsDroppedTogether(t *testing.T) {
	data := awkward(50_000)
	arts := nntptest.Post("st", "file.bin", data, 10_000)
	s := nntptest.Start(t, arts...)
	c, _ := NewClient([]Provider{provider(s, "a", 4)})
	defer c.Close()
	var wg sync.WaitGroup
	for _, a := range arts[:4] { // open four connections at once
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			if _, _, err := c.Body(context.Background(), Live, id); err != nil {
				t.Error(err)
			}
		}(a.ID)
	}
	wg.Wait()
	pl := c.pools[0]
	pl.mu.Lock()
	if len(pl.idle) < 2 {
		pl.mu.Unlock()
		t.Fatalf("want several idle connections, have %d", len(pl.idle))
	}
	for _, cn := range pl.idle {
		cn.lastUsed = time.Now().Add(-time.Minute) // idle a minute
	}
	pl.mu.Unlock()
	s.CloseConns() // the server drops them all
	time.Sleep(50 * time.Millisecond)

	var logged bytes.Buffer
	c.SetLogger(slog.New(slog.NewTextHandler(&logged, nil)))
	got, _, err := c.Body(context.Background(), Live, arts[4].ID)
	if err != nil || !bytes.Equal(got, data[40_000:]) {
		t.Fatalf("read after the server closed idle connections: %v", err)
	}
	if strings.Contains(logged.String(), "provider problem") {
		t.Fatalf("a stale idle connection was reported as a provider problem:\n%s", logged.String())
	}
}
