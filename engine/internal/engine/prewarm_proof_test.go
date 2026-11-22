package engine

import (
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

func prewarmJob(t *testing.T) (*nntptest.Server, *Engine, string) {
	t.Helper()
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	j, _ := e.Submit(nntptest.PostNZB(s, "m", nntptest.File{Name: "Ep.mkv", Data: data(7, 1_280_000)}), "Ep", "tv", "")
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("import: %+v", got)
	}
	e.Cache.Purge()
	return s, e, j.ID
}

func brokenVia(e *Engine, id string) (string, string) {
	j, _, _ := e.Store.Get(id)
	if j.Health == nil || j.Health.Status != health.Broken || len(j.Health.Broken) == 0 {
		return "", ""
	}
	return j.Health.Broken[0].Via, j.Health.Broken[0].Segment
}

// An article every provider lacks, found while prewarming the next
// episode, goes to repair now instead of when the viewer presses play.
func TestPrewarmProofMarksBroken(t *testing.T) {
	s, e, id := prewarmJob(t)
	s.Remove("m0.3@test")
	if err := e.Prewarm(id, "Ep.mkv", 320_000); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if via, seg := brokenVia(e, id); via != "" {
			if via != "prewarm" || seg != "m0.3@test" {
				t.Fatalf("broken via %q segment %q, want prewarm / m0.3@test", via, seg)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("missing article during prewarm not marked broken")
}

// A dropped connection proves nothing: no broken mark.
func TestPrewarmTransientFailureNotProof(t *testing.T) {
	s, e, id := prewarmJob(t)
	s.Drop("m0.3@test")
	if err := e.Prewarm(id, "Ep.mkv", 320_000); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, busy := prewarming.Load(id + "/Ep.mkv"); !busy {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if via, _ := brokenVia(e, id); via != "" {
		t.Fatalf("transient failure marked broken via %q", via)
	}
}
