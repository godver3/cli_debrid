package engine

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// A debrid torrent registered by cli_debrid streams over HTTP ranges with
// links from the callback; an expired/refused link is replaced once.
func TestDebridJobStreamsWithFreshLinks(t *testing.T) {
	file := data(21, 3*1<<20+12345) // 3 chunks + a bit
	var served, refused atomic.Int64
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("v") != "2" { // the first link has "expired"
			refused.Add(1)
			http.Error(w, "expired", http.StatusForbidden)
			return
		}
		a, b := rangeOf(r, len(file))
		served.Add(1)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(file)))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(file[a : b+1])
	}))
	defer cdn.Close()
	var calls atomic.Int64
	cb := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			http.Error(w, "no", 401)
			return
		}
		var req struct {
			Ref     string `json:"ref"`
			Refresh bool   `json:"refresh"`
		}
		json.NewDecoder(r.Body).Decode(&req)
		v := strconv.FormatInt(calls.Add(1), 10)
		json.NewEncoder(w).Encode(map[string]any{"url": cdn.URL + "/" + req.Ref + "?v=" + v, "expires_in": 3600})
	}))
	defer cb.Close()

	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.LinkCallback, c.Token = cb.URL, "tok" })
	j, err := e.AddDebrid("realdebrid-ABC123", "Movie.2024.1080p", "movies", "",
		[]DebridFile{{Name: "Movie.2024.1080p.mkv", Size: int64(len(file)), Ref: "ABC123:1"}})
	if err != nil || j.Status != store.Ready || j.Protocol != ProtocolDebrid {
		t.Fatalf("add: %+v %v", j, err)
	}
	files, err := e.Files(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, res, _ := e.Store.Get(j.ID)
	r := files.Entry(res.Entries[0])
	got := make([]byte, len(file))
	if n, err := r.ReadAt(got, 0); n != len(file) || (err != nil && err != io.EOF) || !bytes.Equal(got, file) {
		t.Fatalf("read: n=%d err=%v equal=%v", n, err, bytes.Equal(got, file))
	}
	if refused.Load() != 1 || calls.Load() != 2 {
		t.Fatalf("expected one refused link then one fresh one: refused=%d callbacks=%d", refused.Load(), calls.Load())
	}
	before := served.Load()
	part := make([]byte, 1000)
	r.ReadAt(part, 2<<20+5)
	if served.Load() != before || !bytes.Equal(part, file[2<<20+5:2<<20+1005]) {
		t.Fatal("a re-read should come from the cache")
	}
	if _, err := e.AddDebrid("bad id!", "x", "", "", []DebridFile{{Name: "a", Size: 1, Ref: "r"}}); err == nil {
		t.Fatal("invalid id accepted")
	}
}

// A connection reset by the CDN (seen live from Real-Debrid on a far seek)
// is retried, not turned into a read error, and needs no new link.
func TestDebridRetriesResetConnections(t *testing.T) {
	file := data(22, 5<<20)
	var resets atomic.Int64
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if resets.Add(1) <= 2 {
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close() // reset mid-request
			return
		}
		a, b := rangeOf(r, len(file))
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(file)))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(file[a : b+1])
	}))
	defer cdn.Close()
	var links atomic.Int64
	cb := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		links.Add(1)
		json.NewEncoder(w).Encode(map[string]any{"url": cdn.URL + "/f", "expires_in": 3600})
	}))
	defer cb.Close()
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.LinkCallback, c.Token = cb.URL, "tok" })
	j, _ := e.AddDebrid("rd-R1", "Reset", "", "rd", []DebridFile{{Name: "r.mkv", Size: int64(len(file)), Ref: "R1:1"}})
	files, _ := e.Files(j.ID)
	_, res, _ := e.Store.Get(j.ID)
	buf := make([]byte, 1000)
	if _, err := files.Entry(res.Entries[0]).ReadAt(buf, 4<<20+7); err != nil || !bytes.Equal(buf, file[4<<20+7:4<<20+1007]) {
		t.Fatalf("read after resets: %v", err)
	}
	if links.Load() != 1 {
		t.Fatalf("a reset should not cost a new link: %d link requests", links.Load())
	}
}

// rangeOf parses "bytes=a-b" or "bytes=a-" (streams ask open-ended).
func rangeOf(r *http.Request, size int) (int64, int64) {
	var a, b int64 = 0, int64(size) - 1
	spec := strings.TrimPrefix(r.Header.Get("Range"), "bytes=")
	if parts := strings.SplitN(spec, "-", 2); len(parts) == 2 {
		fmt.Sscan(parts[0], &a)
		if parts[1] != "" {
			fmt.Sscan(parts[1], &b)
		}
	}
	return a, b
}

// The tree splits like cli_mount's: every job in __all__, usenet in nzbs,
// debrid in torrents and in its provider's folder, broken jobs in __bad__.
func TestTreeSplitsLikeCliMount(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	if _, err := e.AddDebrid("rd-T1", "Show.S01", "tv", "", []DebridFile{{Name: "e1.mkv", Size: 10, Ref: "T1:1"}}); err != nil {
		t.Fatal(err)
	}
	if _, err := e.AddDebrid("x1", "Film", "movies", "torbox", []DebridFile{{Name: "f.mkv", Size: 10, Ref: "X:1"}}); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(e.TopFolders(), ","); got != "__all__,__bad__,torrents,nzbs,realdebrid,torbox" {
		t.Fatalf("tops %s", got)
	}
	names := func(top string) string {
		var out []string
		for _, f := range e.Folders(top) {
			out = append(out, f.Name)
		}
		return strings.Join(out, ",")
	}
	for top, want := range map[string]string{"__all__": "Film,Show.S01", "torrents": "Film,Show.S01", "nzbs": "",
		"realdebrid": "Show.S01", "torbox": "Film", "__bad__": ""} {
		if got := names(top); got != want {
			t.Errorf("%s: got %q want %q", top, got, want)
		}
	}
	e.MarkBroken("rd-T1", "e1.mkv", ReasonHosterGone, "test", "")
	if got := names("__bad__"); got != "Show.S01" {
		t.Errorf("__bad__ after MarkBroken: %q", got)
	}
	e.ClearHealth("rd-T1")
	if got := names("__bad__"); got != "" {
		t.Errorf("__bad__ after ClearHealth: %q", got)
	}
}

// Readers missing the link cache at the same time share one callback:
// each callback mints a new link at the provider.
func TestConcurrentLinkMissesShareOneCallback(t *testing.T) {
	var calls atomic.Int32
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		<-release
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"url":"http://cdn/x","expires_in":3600}`))
	}))
	defer srv.Close()
	c := newCallbackLinks(srv.URL, "t", nil)
	var wg sync.WaitGroup
	urls := make([]string, 8)
	for i := range urls {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			u, err := c.Link(context.Background(), "rd:ABC:2", false)
			if err != nil {
				t.Error(err)
			}
			urls[i] = u
		}(i)
	}
	time.Sleep(100 * time.Millisecond)
	close(release)
	wg.Wait()
	if n := calls.Load(); n != 1 {
		t.Fatalf("callbacks = %d, want 1", n)
	}
	for _, u := range urls {
		if u != "http://cdn/x" {
			t.Fatalf("url = %q", u)
		}
	}
	// cached now: no further callback
	if _, err := c.Link(context.Background(), "rd:ABC:2", false); err != nil || calls.Load() != 1 {
		t.Fatalf("cached link: err=%v calls=%d", err, calls.Load())
	}
}

// A waiter whose own context ends gives up without breaking the shared call.
func TestLinkWaiterContextCancel(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		w.Write([]byte(`{"url":"http://cdn/y","expires_in":3600}`))
	}))
	defer srv.Close()
	c := newCallbackLinks(srv.URL, "t", nil)
	first := make(chan error, 1)
	go func() { _, err := c.Link(context.Background(), "rd:B:1", false); first <- err }()
	time.Sleep(50 * time.Millisecond)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := c.Link(ctx, "rd:B:1", false); err == nil {
		t.Fatal("cancelled waiter got no error")
	}
	close(release)
	if err := <-first; err != nil {
		t.Fatal(err)
	}
}

// Members of a stored zip the provider serves as one file (TorBox's zipped
// big packs) are byte ranges of that file: each reads its own bytes over
// the zip's one link, and a range outside the zip is refused.
func TestDebridZipMembersReadTheirRange(t *testing.T) {
	zip := data(33, 2*1<<20+777)
	var links atomic.Int64
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a, b := rangeOf(r, len(zip))
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", a, b, len(zip)))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(zip[a : b+1])
	}))
	defer cdn.Close()
	cb := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		links.Add(1)
		json.NewEncoder(w).Encode(map[string]any{"url": cdn.URL + "/z", "expires_in": 3600})
	}))
	defer cb.Close()

	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.LinkCallback, c.Token = cb.URL, "tok" })
	size := int64(len(zip))
	members := []DebridFile{
		{Name: "Show.S01E01.mkv", Size: 1000, Ref: "Z:1", Offset: 100, SourceSize: size},
		{Name: "Show.S01E02.mkv", Size: 1<<20 + 50, Ref: "Z:1", Offset: 1<<20 + 600, SourceSize: size},
	}
	j, err := e.AddDebrid("torbox-Z1", "Show.S01", "tv", "torbox", members)
	if err != nil {
		t.Fatal(err)
	}
	files, err := e.Files(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	_, res, _ := e.Store.Get(j.ID)
	for i, m := range members {
		got := make([]byte, m.Size)
		r := files.Entry(res.Entries[i])
		if n, err := r.ReadAt(got, 0); int64(n) != m.Size || (err != nil && err != io.EOF) ||
			!bytes.Equal(got, zip[m.Offset:m.Offset+m.Size]) {
			t.Fatalf("%s: n=%d err=%v", m.Name, n, err)
		}
		if r.Size() != m.Size {
			t.Fatalf("%s: size %d", m.Name, r.Size())
		}
	}
	if links.Load() != 1 {
		t.Fatalf("members of one zip should share its link: %d callbacks", links.Load())
	}

	for _, bad := range [][]DebridFile{
		{{Name: "a.mkv", Size: 10, Ref: "Z:2", Offset: size - 5, SourceSize: size}},
		{{Name: "a.mkv", Size: 10, Ref: "Z:2", Offset: 5}},
	} {
		if _, err := e.AddDebrid("torbox-Z2", "Bad", "", "torbox", bad); err == nil {
			t.Fatalf("accepted %+v", bad[0])
		}
	}
}

func TestUnreachableLinkCallbackIncludesFailedURL(t *testing.T) {
	const ref, failed = "tb:1:0", "https://down.example/path?token=test"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Ref       string `json:"ref"`
			Refresh   bool   `json:"refresh"`
			FailedURL string `json:"unreachable_url"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if body.Ref != ref || !body.Refresh || body.FailedURL != failed {
			t.Errorf("unexpected callback payload: %+v", body)
		}
		io.WriteString(w, `{"url":"https://up.example/path","expires_in":3600}`)
	}))
	defer srv.Close()
	c := newCallbackLinks(srv.URL, "t", nil)
	if u, err := c.RefreshUnreachable(t.Context(), ref, failed); err != nil || u != "https://up.example/path" {
		t.Fatalf("replacement link = %q, %v", u, err)
	}
}
