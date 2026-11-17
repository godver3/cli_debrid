package nntp

import (
	"bytes"
	"context"
	"errors"
	"math/rand"
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
