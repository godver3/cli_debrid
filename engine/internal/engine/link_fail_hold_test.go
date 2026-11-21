package engine

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Found live 2026-11-21: one dead Real-Debrid file was asked for 16 times in
// 15 s, every reader minting again. A refusal is now answered from memory
// for a while; a success clears it.

func linkServer(t *testing.T, status *atomic.Int64, body *atomic.Value) (*httptest.Server, *atomic.Int64) {
	var hits atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(int(status.Load()))
		w.Write([]byte(body.Load().(string)))
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func TestRefusedLinkIsRemembered(t *testing.T) {
	old := linkFailHold
	linkFailHold = 200 * time.Millisecond
	t.Cleanup(func() { linkFailHold = old })
	var status atomic.Int64
	var body atomic.Value
	status.Store(502)
	body.Store(`{"error":"503 Server Error"}`)
	srv, hits := linkServer(t, &status, &body)
	c := newCallbackLinks(srv.URL, "t", nil)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := c.Link(ctx, "rd:T:1", i%2 == 1); err == nil {
			t.Fatal("want an error")
		}
	}
	if hits.Load() != 1 {
		t.Fatalf("callback hit %d times in the hold, want 1", hits.Load())
	}
	if _, err := c.Link(ctx, "rd:OTHER:1", false); err == nil || hits.Load() != 2 {
		t.Fatalf("another ref must still be asked (hits %d)", hits.Load())
	}
	time.Sleep(250 * time.Millisecond)
	status.Store(200)
	body.Store(`{"url":"https://cdn/x","expires_in":60}`)
	if u, err := c.Link(ctx, "rd:T:1", false); err != nil || u != "https://cdn/x" {
		t.Fatalf("after the hold: %q %v", u, err)
	}
	status.Store(502)
	before := hits.Load()
	if _, err := c.Link(ctx, "rd:T:1", true); err == nil {
		t.Fatal("refresh failing must error")
	}
	if _, err := c.Link(ctx, "rd:T:1", true); err == nil || hits.Load() != before+1 {
		t.Fatalf("a second refresh inside the hold must not ask again (hits +%d)", hits.Load()-before)
	}
	// The last good link stays usable for readers that don't need a new one.
	if u, err := c.Link(ctx, "rd:T:1", false); err != nil || u != "https://cdn/x" {
		t.Fatalf("cached link after a failed refresh: %q %v", u, err)
	}
}

func TestDeadLinkIsRememberedLonger(t *testing.T) {
	oldF, oldD := linkFailHold, linkDeadHold
	linkFailHold, linkDeadHold = 50*time.Millisecond, time.Hour
	t.Cleanup(func() { linkFailHold, linkDeadHold = oldF, oldD })
	var status atomic.Int64
	var body atomic.Value
	status.Store(410)
	body.Store(`{"dead":true,"reason":"hoster_unavailable","error":"Real-Debrid error 24"}`)
	srv, hits := linkServer(t, &status, &body)
	c := newCallbackLinks(srv.URL, "t", nil)
	_, err := c.Link(context.Background(), "rd:D:1", false)
	var dead *stream.DeadFileError
	if !errors.As(err, &dead) {
		t.Fatalf("want a DeadFileError, got %v", err)
	}
	time.Sleep(80 * time.Millisecond) // past the plain hold, inside the dead one
	if _, err := c.Link(context.Background(), "rd:D:1", true); !errors.As(err, &dead) || hits.Load() != 1 {
		t.Fatalf("dead file asked again (hits %d, err %v)", hits.Load(), err)
	}
}
