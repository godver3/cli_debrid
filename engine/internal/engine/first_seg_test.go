package engine

import (
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// New imports record each source's first article, so later readers find
// its NZB file by message-ID rather than by position (0.8.2 changed the
// position order and old jobs read the wrong files).
func TestImportRecordsFirstSegment(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	j, err := e.Submit(nntptest.PostNZB(s, "fs", nntptest.File{Name: "Show.S01E02.mkv", Data: data(9, 200_000)}), "Show.S01E02", "tv", "")
	if err != nil {
		t.Fatal(err)
	}
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("import: %+v", got)
	}
	_, res, err := e.Store.Full(j.ID)
	if err != nil || len(res.Sources) == 0 {
		t.Fatalf("result: %v", err)
	}
	if res.Sources[0].FirstSeg != "fs0.1@test" {
		t.Fatalf("first_seg %q", res.Sources[0].FirstSeg)
	}
}
