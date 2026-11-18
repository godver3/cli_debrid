package engine

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

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
