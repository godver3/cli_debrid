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

// A job migrated from cli_mount: its file is sliced out of "volume"
// articles (a header in front, like a RAR member), served byte-exact from
// the map, health-checked through the map, and its id and folder kept.
func TestImportMappedFromCliMount(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.ReadAhead = 2 })
	want := data(11, 200_000)
	copy(want, []byte{0x1A, 0x45, 0xDF, 0xA3})                 // a real MKV start: health checks read the header
	volume := append([]byte("RAR-HEADER-1234567890"), want...) // 21-byte header
	arts := nntptest.Post("vol", "show.part01.rar", volume, 64_000)
	s.Add(arts...)
	var segs []MappedSegment
	var off int64
	for i, a := range arts {
		ds, n := int64(0), int64(64_000)
		if i == 0 {
			ds, n = 21, 64_000-21
		}
		if i == len(arts)-1 {
			n = int64(len(volume)) - int64(i)*64_000
		}
		segs = append(segs, MappedSegment{ID: "<" + a.ID + ">", Start: off, End: off + n - 1, DataStart: ds})
		off += n
	}
	j, err := e.ImportMapped("95af6933-9d46-487a-96a1-efac7d2b4113", "Show.S01E01 & Friends", "tv", []string{"x"},
		[]MappedFileSpec{{Name: "Show.S01E01.mkv", Segments: segs}})
	if err != nil || j.Status != store.Ready || j.ID != "95af6933-9d46-487a-96a1-efac7d2b4113" || j.Name != "Show.S01E01 & Friends" {
		t.Fatalf("import: %+v %v", j, err)
	}
	if f, ok := e.Folder(AllFolder, "Show.S01E01 & Friends"); !ok || len(f.Entries) != 1 || f.Entries[0].Size != int64(len(want)) {
		t.Fatalf("folder: %+v", f)
	}
	files, err := e.Files(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, res, _ := e.Store.Get(j.ID)
	r := files.Entry(res.Entries[0])
	got := make([]byte, len(want))
	if n, err := r.ReadAt(got, 0); n != len(want) || (err != nil && err.Error() != "EOF") || string(got) != string(want) {
		t.Fatalf("full read: n=%d err=%v equal=%v", n, err, string(got) == string(want))
	}
	part := make([]byte, 5000)
	if _, err := r.ReadAt(part, 63_970); err != nil || string(part) != string(want[63_970:68_970]) {
		t.Fatalf("read across an article boundary: %v", err)
	}
	e.CheckHealth(j.ID, true)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if g, _, _ := e.Store.Get(j.ID); g.Health != nil && g.Health.Status == "healthy" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if g, _, _ := e.Store.Get(j.ID); g.Health == nil || g.Health.Status != "healthy" {
		t.Fatalf("health through the map: %+v", g.Health)
	}
	if _, err := e.ImportMapped(j.ID, "again", "", nil, []MappedFileSpec{{Name: "a.mkv", Segments: segs}}); err == nil {
		t.Fatal("duplicate id accepted")
	}
	gap := append([]MappedSegment(nil), segs...)
	gap[1].Start++
	if _, err := e.ImportMapped("b6f0c3a0-0000-4000-8000-000000000001", "Gap", "", nil, []MappedFileSpec{{Name: "a.mkv", Segments: gap}}); err == nil {
		t.Fatal("non-contiguous map accepted")
	}
}
