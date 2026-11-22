package stream

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// Connections to one capped host never exceed the cap.
func TestRemoteConnsPerHostCap(t *testing.T) {
	old, oldHost := RemoteConnsPerHost.Load(), cappedHost
	RemoteConnsPerHost.Store(3)
	cappedHost = func(string) bool { return true } // the test server stands in for a TorBox host
	defer func() { RemoteConnsPerHost.Store(old); cappedHost = oldHost }()
	var active, peak atomic.Int64
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := active.Add(1)
		for p := peak.Load(); n > p && !peak.CompareAndSwap(p, n); p = peak.Load() {
		}
		<-release
		active.Add(-1)
		w.WriteHeader(http.StatusPartialContent)
		io.WriteString(w, "x")
	}))
	defer srv.Close()
	c := NewRemoteClient(nil)
	addr := strings.TrimPrefix(srv.URL, "http://")
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL, nil)
			if resp, err := c.Do(req); err == nil {
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
			}
		}()
	}
	time.Sleep(300 * time.Millisecond)
	if n := sharedCap.inUse(addr); n != 3 {
		t.Fatalf("%d connections open to one host, want the cap of 3", n)
	}
	if p := peak.Load(); p != 3 {
		t.Fatalf("server saw %d at once, want 3", p)
	}
	for i := 0; i < 6; i++ {
		release <- struct{}{}
	}
	wg.Wait()
}

func TestCapOnlyTorBoxHosts(t *testing.T) {
	for host, want := range map[string]bool{
		"store-1.tb-cdn.st": true, "STORE-031.WEUR.TB-CDN.ST": true, "torbox.app": true, "cdn.torbox.app": true,
		"download.real-debrid.com": false, "notb-cdn.st": false, "tb-cdn.st.evil.com": false, "127.0.0.1": false,
	} {
		if got := cappedHost(host); got != want {
			t.Errorf("cappedHost(%q) = %v, want %v", host, got, want)
		}
	}
}

// Hosts other than TorBox's are never held back by the cap.
func TestNonTorBoxHostUncapped(t *testing.T) {
	old := RemoteConnsPerHost.Load()
	RemoteConnsPerHost.Store(1)
	defer RemoteConnsPerHost.Store(old)
	release := make(chan struct{})
	var active atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		active.Add(1)
		<-release
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	c := NewRemoteClient(nil)
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if resp, err := c.Get(srv.URL); err == nil {
				resp.Body.Close()
			}
		}()
	}
	time.Sleep(300 * time.Millisecond)
	if n := active.Load(); n != 3 {
		t.Fatalf("%d requests reached a non-TorBox host with cap 1, want all 3", n)
	}
	close(release)
	wg.Wait()
}
