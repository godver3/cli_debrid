package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"html"
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

type file struct {
	subject string
	data    []byte
}

// nzbXML posts files to the server and returns an NZB document for them.
func nzbXML(s *nntptest.Server, prefix string, files ...file) []byte {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="utf-8"?>` + "\n" + `<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">` + "\n")
	for i, f := range files {
		arts := nntptest.Post(fmt.Sprintf("%s%d", prefix, i), f.subject, f.data, 64_000)
		s.Add(arts...)
		subj := fmt.Sprintf(`[%d/%d] - "%s" yEnc (1/%d)`, i+1, len(files), f.subject, len(arts))
		fmt.Fprintf(&b, ` <file poster="p" date="1700000000" subject="%s">`+"\n  <groups><group>alt.binaries.test</group></groups>\n  <segments>\n", html.EscapeString(subj))
		for n, a := range arts {
			fmt.Fprintf(&b, "   <segment bytes=\"%d\" number=\"%d\">%s</segment>\n", len(a.Body), n+1, a.ID)
		}
		b.WriteString("  </segments>\n </file>\n")
	}
	b.WriteString("</nzb>\n")
	return []byte(b.String())
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
	doc := nzbXML(h.s, "a", file{"Movie.2024.1080p.mkv", movie}, file{"Movie.2024.1080p.nfo", []byte("notes")})

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
	doc2 := nzbXML(h.s, "b", file{"Other.mkv", random(2, 70_000)})
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
		files = append(files, file{fmt.Sprintf("ep.part%02d.rar", i+2), v})
	}
	files = append(files, file{"ep.sample.mkv", random(4, 20_000)})
	var j jobOut
	h.do("POST", "/jobs?name=Ep", nzbXML(h.s, "c", files...), &j)
	if j = h.waitDone(j.ID); j.Status != "failed" || j.Retryable || !strings.Contains(j.Error, "leftovers") {
		t.Fatalf("leftover job: %+v", j)
	}

	// Articles gone everywhere is proof, not network trouble: also final.
	doc := nzbXML(h.s, "d", file{"Gone.mkv", random(5, 70_000)})
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
	doc = nzbXML(h.s, "e", file{"Flaky.mkv", random(6, 70_000)})
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
