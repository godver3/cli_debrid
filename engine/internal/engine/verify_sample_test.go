package engine

import (
	"context"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// Found live 2026-11-21: verifying a replacement STATed every article
// (~5000 for an episode, ~78 s); cli_debrid gives up at 75 s and asked
// again from zero, so replacements sat in "Pending verification" forever.
// It now samples like the health check and cli_mount's verify.
func TestVerifyReplacementSamplesArticles(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	const parts = 300
	j, err := e.Submit(nntptest.PostNZB(s, "v", nntptest.File{Name: "Show.S01E01.mkv", Data: data(7, parts*64_000)}), "Show.S01E01", "tv", "")
	if err != nil {
		t.Fatal(err)
	}
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("import: %+v", got)
	}
	before := s.Stats.Load()
	if _, err := e.VerifyReplacement(context.Background(), 1, j.ID); err != nil {
		t.Fatal(err)
	}
	stats := s.Stats.Load() - before
	if stats == 0 || stats > parts/4 {
		t.Fatalf("verify sent %d STATs for %d articles, want a ~10%% sample", stats, parts)
	}

	// A missing article where verify always looks (the last one) is still proof.
	s.Remove("v0.300@test")
	r, err := e.VerifyReplacement(context.Background(), 1, j.ID)
	if err != nil || r.Status != "broken" || r.Reason != "usenet_segment_missing" {
		t.Fatalf("missing last article: %+v %v", r, err)
	}
}
