package engine

import (
	"fmt"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// A scan that opens every job once must not keep every parsed NZB in memory.
func TestBuiltReadersAreDroppedWhenIdle(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	n := maxOpenJobs + 6
	ids := make([]string, n)
	for i := range ids {
		nz := nntptest.PostNZB(s, fmt.Sprintf("j%d", i), nntptest.File{Name: "Movie.mkv", Data: data(int64(i), 5_000)})
		j, err := e.Submit(nz, fmt.Sprintf("Movie %d", i), "movies", "")
		if err != nil {
			t.Fatal(err)
		}
		ids[i] = j.ID
	}
	for _, id := range ids {
		if j := waitDone(t, e, id); j.Status != store.Ready {
			t.Fatalf("%s: %s %s", id, j.Status, j.Error)
		}
	}
	playing := e.OpenStream(ids[0], "Movie.mkv", 5_000)
	for _, id := range ids {
		if _, err := e.Files(id); err != nil {
			t.Fatal(err)
		}
	}
	if got := e.OpenJobs(); got > maxOpenJobs+1 {
		t.Fatalf("%d jobs keep built readers, cap is %d (+1 playing)", got, maxOpenJobs)
	}
	if e.trimOpen(time.Now().Add(openJobIdle+time.Second)) == 0 || e.OpenJobs() != 1 {
		t.Fatalf("idle readers kept: %d", e.OpenJobs())
	}
	e.mu.Lock()
	_, kept := e.open[ids[0]]
	e.mu.Unlock()
	if !kept {
		t.Fatal("readers of a job with an open file were dropped")
	}
	e.CloseStream(playing)
	e.trimOpen(time.Now().Add(openJobIdle + time.Second))
	if e.OpenJobs() != 0 {
		t.Fatalf("still %d after the file closed", e.OpenJobs())
	}
	if _, err := e.Files(ids[3]); err != nil { // rebuilt on demand
		t.Fatal(err)
	}
}
