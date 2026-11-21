package engine

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

func waitHealth(t *testing.T, e *Engine, id string) *store.Health {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if j, _, _ := e.Store.Get(id); j.Health != nil && j.Health.Status != "checking" {
			return j.Health
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("health check never finished")
	return nil
}

// cli_debrid's callback: /link and /check, answering per file reference.
func callback(t *testing.T, check map[string]any, link map[string]any) string {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Ref string `json:"ref"`
		}
		json.NewDecoder(r.Body).Decode(&req)
		switch r.URL.Path {
		case "/check":
			ans, ok := check[req.Ref]
			if !ok {
				http.Error(w, `{"error":"boom"}`, 500)
				return
			}
			json.NewEncoder(w).Encode(ans)
		case "/link":
			if ans, ok := link[req.Ref]; ok {
				w.WriteHeader(http.StatusGone)
				json.NewEncoder(w).Encode(ans)
				return
			}
			http.Error(w, `{"error":"provider timeout"}`, 502)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL + "/link"
}

func TestDebridCheckMarksOnlyProvenDeadFiles(t *testing.T) {
	cb := callback(t, map[string]any{
		"rd:T:1": map[string]string{"state": "broken", "reason": "hoster_unavailable"},
		"rd:T:2": map[string]string{"state": "healthy"},
		"rd:T:3": map[string]string{"state": "unknown", "reason": "provider_probe_error"},
		// rd:T:4 -> HTTP 500: proves nothing
	}, nil)
	e := newEngine(t, nntptest.Start(t), func(c *Config) { c.LinkCallback, c.Token = cb, "tok" })
	j, err := e.AddDebrid("rd-T", "Show.S01", "tv", "rd", []DebridFile{
		{Name: "e1.mkv", Size: 100, Ref: "rd:T:1"}, {Name: "e2.mkv", Size: 100, Ref: "rd:T:2"},
		{Name: "e3.mkv", Size: 100, Ref: "rd:T:3"}, {Name: "e4.mkv", Size: 100, Ref: "rd:T:4"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := e.CheckHealth(j.ID, false); err != nil {
		t.Fatal(err)
	}
	h := waitHealth(t, e, j.ID)
	if h.Status != health.Broken || len(h.Broken) != 1 || h.Broken[0].Name != "e1.mkv" ||
		h.Broken[0].Reason != "hoster_unavailable" || h.Inconclusive != 2 {
		t.Fatalf("health: %+v", h)
	}
	if _, ok := e.Folder(BadFolder, "Show.S01"); !ok {
		t.Fatal("broken debrid job not in __bad__")
	}
}

func TestDebridCheckAllHealthy(t *testing.T) {
	cb := callback(t, map[string]any{"rd:H:1": map[string]string{"state": "healthy"}}, nil)
	e := newEngine(t, nntptest.Start(t), func(c *Config) { c.LinkCallback, c.Token = cb, "tok" })
	j, _ := e.AddDebrid("rd-H", "Movie", "movies", "rd", []DebridFile{{Name: "m.mkv", Size: 100, Ref: "rd:H:1"}})
	e.CheckHealth(j.ID, false)
	if h := waitHealth(t, e, j.ID); h.Status != health.Healthy || len(h.Broken) != 0 {
		t.Fatalf("health: %+v", h)
	}
}

// A dead answer while making a link reaches the read as DeadFileError (the
// mount marks the file broken on it); a plain failure does not.
func TestDeadLinkIsProofAtRead(t *testing.T) {
	cb := callback(t, nil, map[string]any{"rd:D:1": map[string]any{"dead": true, "reason": "hoster_unavailable", "error": "RD error 24"}})
	e := newEngine(t, nntptest.Start(t), func(c *Config) { c.LinkCallback, c.Token = cb, "tok" })
	j, _ := e.AddDebrid("rd-D", "Movie", "movies", "rd", []DebridFile{
		{Name: "dead.mkv", Size: 100, Ref: "rd:D:1"}, {Name: "flaky.mkv", Size: 100, Ref: "rd:D:2"}})
	fs, err := e.Files(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, res, _ := e.Store.Get(j.ID)
	var dead *stream.DeadFileError
	buf := make([]byte, 10)
	for _, en := range res.Entries {
		_, err := fs.Entry(en).ReadAt(buf, 0)
		isDead := errors.As(err, &dead)
		if want := en.Name == "dead.mkv"; isDead != want || err == nil {
			t.Fatalf("%s: dead=%v err=%v", en.Name, isDead, err)
		}
	}
	if dead.Reason != "hoster_unavailable" {
		t.Fatalf("reason %q", dead.Reason)
	}
}

// cli_mount's sweep reads each media file's start: every article present
// but the wrong container header is proof the file can't play.
func TestHealthCheckReadsTheContainerHeader(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	good := append([]byte{0x1A, 0x45, 0xDF, 0xA3}, data(5, 40_000)...)
	bad := data(6, 40_000)
	bad[0] = 0x00
	for _, c := range []struct {
		name   string
		body   []byte
		broken bool
	}{{"Good", good, false}, {"Bad", bad, true}} {
		nz := nntptest.PostNZB(s, c.name, nntptest.File{Name: c.name + ".mkv", Data: c.body})
		j, _ := e.Submit(nz, c.name, "movies", "")
		if got := waitDone(t, e, j.ID); got.Status != store.Ready {
			t.Fatalf("%s: %s %s", c.name, got.Status, got.Error)
		}
		e.CheckHealth(j.ID, false)
		h := waitHealth(t, e, j.ID)
		if c.broken {
			if h.Status != health.Broken || len(h.Broken) != 1 || h.Broken[0].Reason != ReasonHeaderInvalid {
				t.Fatalf("%s: %+v", c.name, h)
			}
		} else if h.Status != health.Healthy {
			t.Fatalf("%s: %+v", c.name, h)
		}
	}
}

func TestDefaultSampleIsCliMounts(t *testing.T) {
	o := healthOptions(0, false)
	if o.SampleRate != 0.10 || o.Max < 1<<20 || o.Full {
		t.Fatalf("%+v", o)
	}
}

// A debrid job whose result.json is lost is dropped at start, so the
// account mirror re-adds it; a usenet one is kept and reported broken.
func TestLostFileMapsAtStart(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	if _, err := e.AddDebrid("rd-L", "Torrent", "", "rd", []DebridFile{{Name: "a.mkv", Size: 10, Ref: "rd:L:1"}}); err != nil {
		t.Fatal(err)
	}
	nz := nntptest.PostNZB(s, "u", nntptest.File{Name: "Movie.mkv", Data: append([]byte{0x1A, 0x45, 0xDF, 0xA3}, data(9, 5_000)...)})
	u, _ := e.Submit(nz, "Movie", "movies", "")
	waitDone(t, e, u.ID)
	cfg := e.Config()
	e.Close()
	for _, id := range []string{"rd-L", u.ID} {
		if err := os.WriteFile(filepath.Join(cfg.DataDir, "jobs", id, "result.json"), []byte("{"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	e2, err := New(cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer e2.Close()
	if _, _, err := e2.Store.Get("rd-L"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("debrid job kept: %v", err)
	}
	if j, _, err := e2.Store.Get(u.ID); err != nil || j.Health == nil || j.Health.Status != health.Broken ||
		j.Health.Broken[0].Reason != ReasonManifestInvalid {
		t.Fatalf("usenet job: %+v %v", j.Health, err)
	}
}
