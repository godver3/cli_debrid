package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

type file = nntptest.File

func nzbXML(s *nntptest.Server, prefix string, files ...file) []byte {
	return nntptest.PostNZB(s, prefix, files...)
}

func random(seed int64, n int) []byte {
	b := make([]byte, n)
	rand.New(rand.NewSource(seed)).Read(b)
	return b
}

type harness struct {
	t   *testing.T
	s   *nntptest.Server
	cfg engine.Config
	e   *engine.Engine
	srv *httptest.Server
}

func newHarness(t *testing.T) *harness {
	s := nntptest.Start(t)
	host, port := s.Addr()
	h := &harness{t: t, s: s, cfg: engine.Config{
		DataDir: t.TempDir(), CacheDir: t.TempDir(), CacheBytes: 64 << 20,
		MountPath: "/mnt/clid", Token: "secret", ImportWorkers: 2,
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 8, Timeout: 5 * time.Second}},
	}}
	h.start()
	return h
}

func (h *harness) start() {
	e, err := engine.New(h.cfg, nil)
	if err != nil {
		h.t.Fatal(err)
	}
	h.e = e
	h.srv = httptest.NewServer(Handler(e))
	h.t.Cleanup(func() { h.srv.Close(); h.e.Close() })
}

func (h *harness) restart() {
	h.srv.Close()
	h.e.Close()
	h.start()
}

func (h *harness) do(method, path string, body []byte, out any) int {
	req, _ := http.NewRequest(method, h.srv.URL+path, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer secret")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if out != nil && len(b) > 0 {
		if err := json.Unmarshal(b, out); err != nil {
			h.t.Fatalf("%s %s: %v: %s", method, path, err, b)
		}
	}
	return resp.StatusCode
}

func (h *harness) waitDone(id string) jobOut {
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		var j jobOut
		h.do("GET", "/jobs/"+id, nil, &j)
		if j.Status == "ready" || j.Status == "failed" {
			return j
		}
		time.Sleep(20 * time.Millisecond)
	}
	h.t.Fatalf("job %s never finished", id)
	return jobOut{}
}

func TestSubmitImportReadAndManage(t *testing.T) {
	h := newHarness(t)
	movie := random(1, 300_000)
	doc := nzbXML(h.s, "a", file{Name: "Movie.2024.1080p.mkv", Data: movie}, file{Name: "Movie.2024.1080p.nfo", Data: []byte("notes")})

	var j jobOut
	if code := h.do("POST", "/jobs?name=Movie.2024.1080p&category=movies", doc, &j); code != 201 || j.Status != "queued" {
		t.Fatalf("submit: %d %+v", code, j)
	}
	j = h.waitDone(j.ID)
	if j.Status != "ready" || j.Path != "/mnt/clid/__all__/Movie.2024.1080p" || len(j.Entries) != 2 ||
		j.Entries[0].Path != "/mnt/clid/__all__/Movie.2024.1080p/Movie.2024.1080p.mkv" {
		t.Fatalf("ready job: %+v", j)
	}
	fs, err := h.e.Files(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	f, _ := h.e.Folder(engine.AllFolder, "Movie.2024.1080p")
	buf := make([]byte, len(movie))
	if n, err := fs.Entry(f.Entries[0]).ReadAt(buf, 0); n != len(movie) || (err != nil && err != io.EOF) || !bytes.Equal(buf, movie) {
		t.Fatalf("read n=%d err=%v", n, err)
	}

	// Migration keeps the caller's id; reusing it is a conflict.
	doc2 := nzbXML(h.s, "b", file{Name: "Other.mkv", Data: random(2, 70_000)})
	if code := h.do("POST", "/jobs?name=Movie.2024.1080p&id=cli-mount-uuid-1", doc2, &j); code != 201 || j.ID != "cli-mount-uuid-1" {
		t.Fatalf("submit with id: %d %+v", code, j)
	}
	if code := h.do("POST", "/jobs?id=cli-mount-uuid-1", doc2, nil); code != 409 {
		t.Fatalf("duplicate id: %d", code)
	}
	if j = h.waitDone("cli-mount-uuid-1"); j.Path != "/mnt/clid/__all__/Movie.2024.1080p (2)" {
		t.Fatalf("same name needs a unique folder: %q", j.Path)
	}

	// Rename, tags, cli_ids (merged).
	h.do("PATCH", "/jobs/cli-mount-uuid-1", []byte(`{"name":"Other.Movie","cli_ids":{"Other.mkv":7}}`), &j)
	h.do("PATCH", "/jobs/cli-mount-uuid-1", []byte(`{"tags":["plex"],"cli_ids":{"x.mkv":8}}`), &j)
	if j.Path != "/mnt/clid/__all__/Other.Movie" || j.CLIIDs["Other.mkv"] != 7 || j.CLIIDs["x.mkv"] != 8 || j.Tags[0] != "plex" {
		t.Fatalf("patched: %+v", j)
	}

	// Changes feed, delete, tombstone.
	var ch struct {
		Now     int64            `json:"now"`
		Jobs    []jobOut         `json:"jobs"`
		Deleted []map[string]any `json:"deleted"`
	}
	h.do("GET", "/changes", nil, &ch)
	if len(ch.Jobs) != 2 {
		t.Fatalf("changes: %d jobs", len(ch.Jobs))
	}
	since := ch.Now
	time.Sleep(5 * time.Millisecond)
	if code := h.do("DELETE", "/jobs/cli-mount-uuid-1", nil, nil); code != 204 {
		t.Fatalf("delete %d", code)
	}
	h.do("GET", fmt.Sprintf("/changes?since=%d", since), nil, &ch)
	if len(ch.Jobs) != 0 || len(ch.Deleted) != 1 || ch.Deleted[0]["id"] != "cli-mount-uuid-1" {
		t.Fatalf("changes after delete: %+v", ch)
	}
	if code := h.do("GET", "/jobs/cli-mount-uuid-1", nil, nil); code != 404 {
		t.Fatalf("deleted job still there: %d", code)
	}

	// Restart: the ready job and its folder come back without usenet.
	before := h.s.Bodies.Load()
	h.restart()
	if j = h.waitDone(f.JobID); j.Path != "/mnt/clid/__all__/Movie.2024.1080p" {
		t.Fatalf("after restart: %+v", j)
	}
	if h.s.Bodies.Load() != before {
		t.Fatal("restart re-downloaded articles")
	}
}

func TestRejectsAndFailures(t *testing.T) {
	h := newHarness(t)
	if code := h.do("POST", "/jobs", []byte("not xml"), nil); code != 400 {
		t.Fatalf("bad nzb: %d", code)
	}
	req, _ := http.NewRequest("GET", h.srv.URL+"/status", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 401 {
		t.Fatalf("wrong token: %d", resp.StatusCode)
	}

	// RAR set without its first volume plus a loose sample: rejected, final.
	vols := rartest.Write(rartest.RAR5, []rartest.Member{{Name: "Ep.mkv", Data: random(3, 400_000)}}, 90_000)
	var files []file
	for i, v := range vols[1:] {
		files = append(files, file{Name: fmt.Sprintf("ep.part%02d.rar", i+2), Data: v})
	}
	files = append(files, file{Name: "ep.sample.mkv", Data: random(4, 20_000)})
	var j jobOut
	h.do("POST", "/jobs?name=Ep", nzbXML(h.s, "c", files...), &j)
	if j = h.waitDone(j.ID); j.Status != "failed" || j.Retryable || !strings.Contains(j.Error, "leftovers") {
		t.Fatalf("leftover job: %+v", j)
	}

	// Articles gone everywhere is proof, not network trouble: also final.
	doc := nzbXML(h.s, "d", file{Name: "Gone.mkv", Data: random(5, 70_000)})
	h.s.Remove("d0.1@test")
	h.do("POST", "/jobs?name=Gone", doc, &j)
	if j = h.waitDone(j.ID); j.Status != "failed" {
		t.Fatalf("missing-article job: %+v", j)
	}
	if j.Retryable {
		t.Fatalf("articles missing everywhere must be final: %+v", j)
	}

	// A provider that drops the connection proves nothing: retryable, and
	// a retry once it is back succeeds.
	doc = nzbXML(h.s, "e", file{Name: "Flaky.mkv", Data: random(6, 70_000)})
	h.s.Drop("e0.1@test")
	h.do("POST", "/jobs?name=Flaky", doc, &j)
	if j = h.waitDone(j.ID); j.Status != "failed" || !j.Retryable {
		t.Fatalf("flaky job: %+v", j)
	}
	h.s.Undrop("e0.1@test")
	if code := h.do("POST", "/jobs/"+j.ID+"/retry", nil, &j); code != 200 {
		t.Fatalf("retry: %d %+v", code, j)
	}
	if j = h.waitDone(j.ID); j.Status != "ready" {
		t.Fatalf("retried job: %+v", j)
	}
}

type healthOut struct {
	Status string `json:"status"`
	Broken []struct {
		Name    string `json:"file_name"`
		Segment string `json:"segment"`
		Via     string `json:"via"`
	} `json:"broken_files"`
	Inconclusive int `json:"inconclusive"`
}

func (h *harness) waitHealth(id string) healthOut {
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		var out healthOut
		h.do("GET", "/jobs/"+id+"/health", nil, &out)
		if out.Status != "checking" && out.Status != "" {
			return out
		}
		time.Sleep(20 * time.Millisecond)
	}
	h.t.Fatalf("health check of %s never finished", id)
	return healthOut{}
}

func TestHealthForRepair(t *testing.T) {
	h := newHarness(t)
	var a, b jobOut
	h.do("POST", "/jobs?name=A", nzbXML(h.s, "ha", file{Name: "A.mkv", Data: random(10, 400_000)}), &a)
	h.do("POST", "/jobs?name=B", nzbXML(h.s, "hb", file{Name: "B.mkv", Data: random(11, 400_000)}), &b)
	h.waitDone(a.ID)
	h.waitDone(b.ID)

	if code := h.do("POST", "/jobs/"+a.ID+"/health?full=true", nil, nil); code != 202 {
		t.Fatalf("start check: %d", code)
	}
	if got := h.waitHealth(a.ID); got.Status != "healthy" {
		t.Fatalf("A: %+v", got)
	}

	// An article gone from every provider: B is broken, with the exact article.
	h.s.Remove("hb0.4@test")
	h.do("POST", "/jobs/"+b.ID+"/health?full=1", nil, nil)
	got := h.waitHealth(b.ID)
	if got.Status != "broken" || len(got.Broken) != 1 || got.Broken[0].Name != "B.mkv" ||
		got.Broken[0].Segment != "hb0.4@test" || got.Broken[0].Via != "check" {
		t.Fatalf("B: %+v", got)
	}
	var broken []jobOut
	h.do("GET", "/health?status=broken", nil, &broken)
	if len(broken) != 1 || broken[0].ID != b.ID || broken[0].Health == nil {
		t.Fatalf("broken list: %+v", broken)
	}

	// Repair done: cli_debrid clears the record.
	if code := h.do("DELETE", "/jobs/"+b.ID+"/health", nil, nil); code != 204 {
		t.Fatalf("clear: %d", code)
	}
	h.do("GET", "/health?status=broken", nil, &broken)
	if len(broken) != 0 {
		t.Fatalf("still listed: %+v", broken)
	}

	// A provider dropping connections proves nothing: never broken.
	h.s.Drop("ha0.1@test")
	h.s.Drop("ha0.7@test")
	h.do("POST", "/jobs/"+a.ID+"/health?full=1", nil, nil)
	if got := h.waitHealth(a.ID); got.Status == "broken" || got.Inconclusive == 0 {
		t.Fatalf("flaky provider: %+v", got)
	}

	// Sweep over every ready job.
	var run struct {
		RunID  string `json:"run_id"`
		Status string `json:"status"`
		Total  int    `json:"total"`
		Done   int    `json:"done"`
	}
	h.do("POST", "/health/run?ignore_last_checked=true&full=true", nil, &run) // sampled sweeps may miss one article; full ones may not
	if run.Total != 2 {
		t.Fatalf("run: %+v", run)
	}
	deadline := time.Now().Add(10 * time.Second)
	for run.Status != "completed" && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
		h.do("GET", "/health/run", nil, &run)
	}
	if run.Status != "completed" || run.Done != 2 {
		t.Fatalf("run end: %+v", run)
	}
	if got := h.waitHealth(b.ID); got.Status != "broken" {
		t.Fatalf("sweep should re-find B broken: %+v", got)
	}
	if code := h.do("POST", "/jobs/nope/health", nil, nil); code != 404 {
		t.Fatalf("unknown job: %d", code)
	}
}
