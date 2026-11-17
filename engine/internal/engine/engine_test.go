package engine

import (
	"math/rand"
	"strings"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

func newEngine(t *testing.T, s *nntptest.Server, tweak func(*Config)) *Engine {
	t.Helper()
	host, port := s.Addr()
	cfg := Config{DataDir: t.TempDir(), MountPath: "unused",
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 4, Timeout: 3 * time.Second}}}
	if tweak != nil {
		tweak(&cfg)
	}
	e, err := New(cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { e.Close() })
	return e
}

func waitDone(t *testing.T, e *Engine, id string) store.Job {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		j, _, _ := e.Store.Get(id)
		if j.Status == store.Ready || j.Status == store.Failed {
			return j
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("import never finished")
	return store.Job{}
}

func data(seed int64, n int) []byte {
	b := make([]byte, n)
	rand.New(rand.NewSource(seed)).Read(b)
	return b
}

func TestImportSampleFailsProvenMissingArticles(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.ImportSample = 100 })
	nzbData := nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.mkv", Data: data(1, 900_000)})
	s.Remove("m0.4@test") // a middle article: the import probe alone would not notice
	j, _ := e.Submit(nzbData, "Movie", "movies", "")
	got := waitDone(t, e, j.ID)
	if got.Status != store.Failed || got.Retryable || !strings.Contains(got.Error, "missing on every provider") ||
		!strings.Contains(got.Error, "m0.4@test") {
		t.Fatalf("got %s retryable=%v err=%q", got.Status, got.Retryable, got.Error)
	}
}

func TestImportSampleOffStillImports(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	nzbData := nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.mkv", Data: data(2, 900_000)})
	s.Remove("m0.4@test")
	j, _ := e.Submit(nzbData, "Movie", "movies", "")
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("without the sample a middle miss is found at playback, not import: %s %q", got.Status, got.Error)
	}
}

func TestImportTimeoutIsRetryable(t *testing.T) {
	s := nntptest.Start(t)
	// One connection and three files: the probes queue behind each other,
	// and the deadline passes while they wait. (A fetch already on the wire
	// finishes; the connection's own read timeout covers hung servers.)
	e := newEngine(t, s, func(c *Config) {
		c.ImportTimeout, c.Providers[0].MaxConns = 1, 1
		c.RetryBackoff = []time.Duration{50 * time.Millisecond}
	})
	nzbData := nntptest.PostNZB(s, "m", nntptest.File{Name: "a.mkv", Data: data(3, 300_000)},
		nntptest.File{Name: "b.mkv", Data: data(5, 300_000)}, nntptest.File{Name: "c.mkv", Data: data(6, 300_000)})
	s.SetDelay(800 * time.Millisecond)
	j, _ := e.Submit(nzbData, "Movie", "movies", "")
	// First an automatic retry (the job stays queued, so cli_debrid keeps
	// waiting instead of blacklisting), then a retryable failure.
	got := waitDone(t, e, j.ID)
	if got.Status != store.Failed || !got.Retryable || got.Attempts != 1 || !strings.Contains(got.Error, "longer than 1s") {
		t.Fatalf("got %s retryable=%v attempts=%d err=%q", got.Status, got.Retryable, got.Attempts, got.Error)
	}
	if again, err := e.Retry(j.ID); err != nil || again.Attempts != 0 {
		t.Fatalf("manual retry should reset attempts: %+v %v", again, err)
	}
}

func TestNothingStreamableSaysWhy(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	nzbData := nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.mkv", Data: data(4, 300_000)})
	s.Remove("m0.1@test") // first article: the file can't even be probed
	j, _ := e.Submit(nzbData, "Movie", "movies", "")
	got := waitDone(t, e, j.ID)
	if got.Status != store.Failed || !strings.Contains(got.Error, "Movie.mkv: ") {
		t.Fatalf("error should name the failed file: %q", got.Error)
	}
}

func TestPrewarmFetchesTheStartOnly(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	j, _ := e.Submit(nntptest.PostNZB(s, "m", nntptest.File{Name: "Ep.mkv", Data: data(7, 1_280_000)}), "Ep", "tv", "")
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("import: %+v", got)
	}
	if err := e.Prewarm(j.ID, "nope.mkv", 1); err == nil {
		t.Fatal("unknown file accepted")
	}
	if err := e.Prewarm(j.ID, "Ep.mkv", 320_000); err != nil { // 5 of 20 articles
		t.Fatal(err)
	}
	want := map[string]bool{"m0.1@test": true, "m0.2@test": true, "m0.3@test": true, "m0.4@test": true, "m0.5@test": true}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		got := map[string]bool{}
		for _, id := range s.BodyOrder() {
			got[id] = true
		}
		done := true
		for id := range want {
			done = done && got[id]
		}
		if done {
			if got["m0.10@test"] {
				t.Fatal("prewarm fetched past the requested bytes")
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("prewarm did not fetch the start: %v", s.BodyOrder())
}
