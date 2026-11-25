package mount

import (
	"bytes"
	"io"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

func readyEngine(t *testing.T, data []byte) (*engine.Engine, string) {
	t.Helper()
	s := nntptest.Start(t)
	host, port := s.Addr()
	e, err := engine.New(engine.Config{DataDir: t.TempDir(), MountPath: "unused",
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 4, Timeout: 5 * time.Second}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Close)
	j, err := e.Submit(nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.mkv", Data: data}), "Movie", "movies", "")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 1000; i++ {
		if g, _, _ := e.Store.Get(j.ID); g.Status == store.Ready {
			return e, j.ID
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("import timed out")
	return nil, ""
}

func davDo(t *testing.T, srv *httptest.Server, method, path string, hdr map[string]string, auth bool) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(method, srv.URL+path, nil)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	if auth {
		req.SetBasicAuth("u", "p")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func TestWebDAVServesTreeReadOnly(t *testing.T) {
	data := make([]byte, 400_000)
	rand.New(rand.NewSource(3)).Read(data)
	e, id := readyEngine(t, data)
	if err := e.PutSidecar(id, "Movie.en.srt", []byte("1\n00:00:01,000 --> 00:00:02,000\nhi\n")); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(WebDAVHandler(e, WebDAVOptions{User: "u", Password: "p"}, nil))
	defer srv.Close()

	if r := davDo(t, srv, "PROPFIND", "/__all__/Movie/", map[string]string{"Depth": "1"}, false); r.StatusCode != 401 {
		t.Fatalf("no auth: %d", r.StatusCode)
	}
	r := davDo(t, srv, "PROPFIND", "/__all__/Movie/", map[string]string{"Depth": "1"}, true)
	body, _ := io.ReadAll(r.Body)
	if r.StatusCode != 207 || !strings.Contains(string(body), "Movie.mkv") || !strings.Contains(string(body), "Movie.en.srt") ||
		!strings.Contains(string(body), "video/x-matroska") {
		t.Fatalf("propfind %d: %s", r.StatusCode, body)
	}
	if st := e.Streams(); len(st) != 0 {
		t.Fatalf("PROPFIND opened a stream: %+v", st)
	}
	r = davDo(t, srv, "GET", "/__all__/Movie/Movie.mkv", map[string]string{"Range": "bytes=1000-200999"}, true)
	got, _ := io.ReadAll(r.Body)
	if r.StatusCode != 206 || !bytes.Equal(got, data[1000:201000]) {
		t.Fatalf("range GET %d, %d bytes, equal %v", r.StatusCode, len(got), bytes.Equal(got, data[1000:201000]))
	}
	r = davDo(t, srv, "GET", "/__all__/Movie/Movie.en.srt", nil, true)
	if got, _ := io.ReadAll(r.Body); !strings.Contains(string(got), "hi") {
		t.Fatalf("sidecar GET: %q", got)
	}
	for _, m := range []string{"PUT", "DELETE", "MKCOL", "MOVE"} {
		if r := davDo(t, srv, m, "/__all__/Movie/x.srt", nil, true); r.StatusCode != 403 {
			t.Fatalf("%s allowed: %d", m, r.StatusCode)
		}
	}
}

func TestSidecarRules(t *testing.T) {
	data := make([]byte, 100_000)
	rand.New(rand.NewSource(4)).Read(data)
	e, id := readyEngine(t, data)
	for _, bad := range []string{"x.mkv", "../x.srt", ".hidden.srt", "a/b.srt", "Movie.mkv"} {
		if err := e.PutSidecar(id, bad, []byte("x")); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
	if err := e.PutSidecar(id, "Movie.en.srt", []byte("one")); err != nil {
		t.Fatal(err)
	}
	if r := resolve(e, "/__all__/movie/MOVIE.EN.SRT"); r.kind != kindSidecar || r.sidecar.Size != 3 {
		t.Fatalf("resolve sidecar: %+v", r)
	}
	if err := e.Delete(id); err != nil {
		t.Fatal(err)
	}
	if p, _ := e.SidecarPath(id, "Movie.en.srt"); fileExists(p) {
		t.Fatal("sidecar outlived its job")
	}
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
