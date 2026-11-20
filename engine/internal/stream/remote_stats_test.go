package stream

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// The Mount page shows debrid next to usenet: requests, bytes and errors
// per provider (the ref prefix).
func TestRemoteStatsPerProvider(t *testing.T) {
	resetRemoteStats()
	defer resetRemoteStats()
	file := bytes.Repeat([]byte("abcdefghij"), 50_000)
	cdn := httptest.NewServer(serveRanges(file, 0, nil))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(64<<20), "rd:ABC:1", int64(len(file)), ReadAhead{})
	buf := make([]byte, 4096)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Prefetch, buf, 1000); err != nil {
		t.Fatal(err)
	}
	refused := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusForbidden) }))
	defer refused.Close()
	bad := OpenRemote(staticLink(refused.URL), nil, NewCache(64<<20), "tb:X:0", 1000, ReadAhead{})
	if _, err := bad.ReadAtCtx(context.Background(), nntp.Prefetch, buf[:10], 0); err == nil {
		t.Fatal("a refused link read succeeded")
	}
	st := map[string]RemoteProviderStats{}
	for _, s := range RemoteStats() {
		st[s.Provider] = s
	}
	rd, tb := st["rd"], st["tb"]
	if rd.Requests < 1 || rd.Bytes < int64(len(buf)) || rd.Errors != 0 || rd.Open != 0 || rd.LatencyMs <= 0 {
		t.Fatalf("rd stats: %+v", rd)
	}
	if tb.Requests < 1 || tb.Errors < 1 || tb.LastError == "" {
		t.Fatalf("tb stats: %+v", tb)
	}
}
