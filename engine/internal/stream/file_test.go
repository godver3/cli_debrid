package stream

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

func setup(t *testing.T, size, part int) ([]byte, []nntptest.Article, *nntptest.Server, *nntp.Client, nzb.File) {
	t.Helper()
	data := make([]byte, size)
	rand.New(rand.NewSource(int64(size))).Read(data)
	arts := nntptest.Post("m", "movie.mkv", data, part)
	s := nntptest.Start(t, arts...)
	host, port := s.Addr()
	c, err := nntp.NewClient([]nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 4, Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	f := nzb.File{Name: "movie.mkv", Total: len(arts)}
	for i, a := range arts {
		f.Segments = append(f.Segments, nzb.Segment{ID: a.ID, Number: i + 1, Bytes: len(a.Body)})
	}
	return data, arts, s, c, f
}

func TestRandomReadsMatchSource(t *testing.T) {
	data, _, _, c, f := setup(t, 2_500_000, 700_000)
	cache := NewCache(64 << 20)
	l, err := Probe(context.Background(), c, cache, f)
	if err != nil {
		t.Fatal(err)
	}
	if l.Size != int64(len(data)) || l.PartSize != 700_000 {
		t.Fatalf("layout %+v", l)
	}
	r := Open(c, cache, f, l, ReadAhead{})
	rnd := rand.New(rand.NewSource(7))
	for i := 0; i < 200; i++ {
		off := rnd.Int63n(int64(len(data)))
		n := 1 + rnd.Intn(1_600_000) // often spans two or three segments
		buf := make([]byte, n)
		got, err := r.ReadAt(buf, off)
		want := data[off:]
		if len(want) > n {
			want = want[:n]
		}
		if got != len(want) || !bytes.Equal(buf[:got], want) {
			t.Fatalf("read %d@%d: got %d bytes (err %v), mismatch", n, off, got, err)
		}
		if got < n && err != io.EOF {
			t.Fatalf("short read at end must be io.EOF, got %v", err)
		}
	}
	if _, err := r.ReadAt(make([]byte, 1), int64(len(data))); err != io.EOF {
		t.Fatalf("read at size: %v", err)
	}
}

func TestWrongArticleIsALayoutErrorNotWrongBytes(t *testing.T) {
	_, arts, s, c, f := setup(t, 2_100_000, 700_000)
	// The server answers segment 2's id with segment 3's body: same size class,
	// wrong place in the file. Serving it would silently corrupt playback.
	s.Add(nntptest.Article{ID: arts[1].ID, Body: arts[2].Body})
	cache := NewCache(64 << 20)
	l, err := Probe(context.Background(), c, cache, f)
	if err != nil {
		t.Fatal(err)
	}
	r := Open(c, cache, f, l, ReadAhead{})
	_, err = r.ReadAt(make([]byte, 10), 700_000)
	var se *SegmentError
	if !errors.Is(err, ErrLayout) || !errors.As(err, &se) || se.Number != 2 {
		t.Fatalf("got %v, want ErrLayout on segment 2", err)
	}
	if nntp.IsNotFound(err) {
		t.Fatal("a wrong article is not proof the right one is missing")
	}
}

func TestSegmentMissingFromNZBIsProof(t *testing.T) {
	_, _, _, c, f := setup(t, 2_100_000, 700_000)
	f.Segments = append(f.Segments[:1], f.Segments[2:]...) // NZB lacks segment 2
	f.Missing = []int{2}
	cache := NewCache(64 << 20)
	l, err := Probe(context.Background(), c, cache, f)
	if err != nil {
		t.Fatal(err)
	}
	_, err = Open(c, cache, f, l, ReadAhead{}).ReadAt(make([]byte, 10), 800_000)
	if !nntp.IsNotFound(err) {
		t.Fatalf("got %v, want proof of absence", err)
	}
}

func TestReadAheadMakesNextReadFree(t *testing.T) {
	data, _, s, c, f := setup(t, 3_500_000, 700_000) // 5 segments
	cache := NewCache(64 << 20)
	l, _ := Probe(context.Background(), c, cache, f)
	r := Open(c, cache, f, l, ReadAhead{Segments: 3})
	buf := make([]byte, 1000)
	if _, err := r.ReadAt(buf, 0); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for s.Bodies.Load() < 4 && time.Now().Before(deadline) { // probe + 3 prefetched on the wire
		time.Sleep(10 * time.Millisecond)
	}
	if got := s.Bodies.Load(); got < 4 {
		t.Fatalf("read-ahead fetched %d bodies, want >= 4", got)
	}
	if _, err := r.ReadAt(buf, 2*700_000+5); err != nil || !bytes.Equal(buf, data[2*700_000+5:2*700_000+1005]) {
		t.Fatalf("read after read-ahead: %v", err)
	}
	// The second read starts its own read-ahead (segment 5), so count only
	// segment 3's fetches.
	seg3 := 0
	for _, id := range s.BodyOrder() {
		if id == f.Segments[2].ID {
			seg3++
		}
	}
	if seg3 != 1 {
		t.Fatalf("segment 3 fetched %d times, want once (by the read-ahead)", seg3)
	}
}

func TestCacheEvictsAndCollapsesConcurrentFetches(t *testing.T) {
	c := NewCache(10)
	var calls atomic.Int32
	var wg sync.WaitGroup
	gate := make(chan struct{})
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = c.Do("k", nntp.Prefetch, func(context.Context) ([]byte, error) {
				calls.Add(1)
				<-gate
				return []byte("12345"), nil
			})
		}()
	}
	time.Sleep(50 * time.Millisecond)
	close(gate)
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatalf("fetches = %d, want 1", calls.Load())
	}
	_, _ = c.Do("k2", nntp.Live, func(context.Context) ([]byte, error) { return []byte("abcdefgh"), nil })
	if _, ok := c.Get("k"); ok {
		t.Fatal("k should have been evicted by the 10-byte budget")
	}
	if n, used := c.Len(); n != 1 || used != 8 {
		t.Fatalf("cache len=%d used=%d", n, used)
	}
}

func TestLiveDoesNotWaitOnQueuedPrefetch(t *testing.T) {
	c := NewCache(1 << 20)
	release := make(chan struct{})
	go func() {
		_, _ = c.Do("k", nntp.Prefetch, func(context.Context) ([]byte, error) { <-release; return []byte("slow"), nil })
	}()
	time.Sleep(30 * time.Millisecond)
	done := make(chan []byte, 1)
	go func() {
		d, _ := c.Do("k", nntp.Live, func(context.Context) ([]byte, error) { return []byte("fast"), nil })
		done <- d
	}()
	select {
	case d := <-done:
		if string(d) != "fast" {
			t.Fatalf("got %q", d)
		}
	case <-time.After(time.Second):
		t.Fatal("live read waited on the prefetch")
	}
	close(release)
}

func TestLiveJoinsPrefetchAlreadyOnTheWire(t *testing.T) {
	c := NewCache(1 << 20)
	release := make(chan struct{})
	var fetches atomic.Int32
	go func() {
		_, _ = c.Do("k", nntp.Prefetch, func(ctx context.Context) ([]byte, error) {
			fetches.Add(1)
			nntp.FireAcquireHook(ctx) // what the NNTP client does on getting a connection
			<-release
			return []byte("data"), nil
		})
	}()
	time.Sleep(30 * time.Millisecond)
	done := make(chan []byte, 1)
	go func() {
		d, _ := c.Do("k", nntp.Live, func(context.Context) ([]byte, error) {
			fetches.Add(1)
			return []byte("dup"), nil
		})
		done <- d
	}()
	time.Sleep(30 * time.Millisecond)
	close(release)
	if d := <-done; string(d) != "data" || fetches.Load() != 1 {
		t.Fatalf("live got %q after %d fetches; want to share the running prefetch", d, fetches.Load())
	}
}

func TestReadAheadBytesAndPerFileCap(t *testing.T) {
	if got := (ReadAhead{Bytes: 1_400_001, Segments: 9}).segments(700_000); got != 3 {
		t.Fatalf("1.4MB+1 over 700k articles = %d segments, want 3 (bytes win over segments)", got)
	}
	_, _, s, c, f := setup(t, 3_500_000, 700_000) // 5 segments
	cache := NewCache(64 << 20)
	l, _ := Probe(context.Background(), c, cache, f)
	s.SetDelay(400 * time.Millisecond)
	r := Open(c, cache, f, l, ReadAhead{Segments: 4, MaxConns: 1})
	if _, err := r.ReadAt(make([]byte, 10), 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(150 * time.Millisecond)
	r.mu.Lock()
	inFlight := len(r.prefetched)
	r.mu.Unlock()
	if inFlight != 1 || len(r.slots) != 1 {
		t.Fatalf("with MaxConns 1, %d prefetches started (%d slots used), want 1", inFlight, len(r.slots))
	}
}

// With a second server, the wrong copy on the first is skipped and the read
// gets the right bytes from the second (found live on a migrated job).
func TestWrongArticleOnOneServerReadsFromTheNext(t *testing.T) {
	data, arts, s, _, f := setup(t, 2_100_000, 700_000)
	s.Add(nntptest.Article{ID: arts[1].ID, Body: arts[2].Body})
	good := nntptest.Start(t, arts...)
	h1, p1 := s.Addr()
	h2, p2 := good.Addr()
	c, err := nntp.NewClient([]nntp.Provider{
		{Name: "bad", Host: h1, Port: p1, MaxConns: 2, Timeout: 5 * time.Second},
		{Name: "good", Host: h2, Port: p2, MaxConns: 2, Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	cache := NewCache(64 << 20)
	l, err := Probe(context.Background(), c, cache, f)
	if err != nil {
		t.Fatal(err)
	}
	r := Open(c, cache, f, l, ReadAhead{})
	buf := make([]byte, 10)
	if _, err := r.ReadAt(buf, 700_000); err != nil || !bytes.Equal(buf, data[700_000:700_010]) {
		t.Fatalf("got %v; want segment 2 from the second server", err)
	}
}
