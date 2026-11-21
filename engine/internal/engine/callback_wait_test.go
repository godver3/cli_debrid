package engine

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// cli_debrid's web app restarting (connection refused for a while) must
// not fail a debrid file opened meanwhile: the link request waits for it.
func TestLinkWaitsWhileCliDebridRestarts(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // nothing listening: "connection refused", as during a restart

	go func() {
		time.Sleep(2500 * time.Millisecond) // back up
		l, err := net.Listen("tcp", addr)
		if err != nil {
			t.Error(err)
			return
		}
		srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Write([]byte(`{"url":"http://cdn/y","expires_in":3600}`))
		}))
		srv.Listener = l
		srv.Start()
		t.Cleanup(srv.Close)
	}()
	c := newCallbackLinks("http://"+addr+"/mount/internal/link", "t", nil)
	start := time.Now()
	u, err := c.Link(context.Background(), "rd:ABC:1", false)
	if err != nil || u != "http://cdn/y" {
		t.Fatalf("got %q, %v", u, err)
	}
	if d := time.Since(start); d < 2*time.Second {
		t.Fatalf("returned after %s, before cli_debrid was back", d)
	}
}

// It gives up after callbackWait, and a reader's own deadline still ends
// its wait sooner.
func TestLinkWaitIsBounded(t *testing.T) {
	old := callbackWait
	callbackWait = 3 * time.Second
	t.Cleanup(func() { callbackWait = old })
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := ln.Addr().String()
	ln.Close()
	c := newCallbackLinks("http://"+addr+"/link", "t", nil)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	start := time.Now()
	if _, err := c.Link(ctx, "rd:X:1", false); err == nil || time.Since(start) > 2*time.Second {
		t.Fatalf("reader deadline: %v after %s", err, time.Since(start))
	}
	start = time.Now()
	if _, err := c.Link(context.Background(), "rd:X:2", false); err == nil || time.Since(start) > 5*time.Second {
		t.Fatalf("callbackWait: %v after %s", err, time.Since(start))
	}
}
