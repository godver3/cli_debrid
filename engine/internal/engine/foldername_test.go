package engine

import (
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// Two jobs with one name: the second is "Name (2)". Deleting the first
// must not hand its name to the second (a symlink to "Name" would then
// play the other release, and links to "Name (2)" would break).
func TestFolderNamesStayWhenTheFirstJobGoes(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	submit := func(prefix string, seed int64) string {
		nz := nntptest.PostNZB(s, prefix, nntptest.File{Name: "Movie.mkv", Data: data(seed, 5_000)})
		j, err := e.Submit(nz, "Movie.2020", "movies", "")
		if err != nil {
			t.Fatal(err)
		}
		if got := waitDone(t, e, j.ID); got.Status != store.Ready {
			t.Fatalf("%s %s", got.Status, got.Error)
		}
		return j.ID
	}
	first := submit("a", 1)
	second := submit("b", 2)
	if e.folderName(first) != "Movie.2020" || e.folderName(second) != "Movie.2020 (2)" {
		t.Fatalf("names: %q %q", e.folderName(first), e.folderName(second))
	}
	if err := e.Delete(first); err != nil {
		t.Fatal(err)
	}
	if got := e.folderName(second); got != "Movie.2020 (2)" {
		t.Fatalf("second job's folder became %q after the first was deleted", got)
	}
	if _, ok := e.Folder(AllFolder, "Movie.2020"); ok {
		t.Fatal("deleted job's folder still listed")
	}
	// The saved name survives a restart.
	cfg := e.Config()
	e.Close()
	e2, err := New(cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer e2.Close()
	if got := e2.folderName(second); got != "Movie.2020 (2)" {
		t.Fatalf("after restart: %q", got)
	}
	// A third job with the name takes the free plain name.
	nz := nntptest.PostNZB(s, "c", nntptest.File{Name: "Movie.mkv", Data: data(3, 5_000)})
	j, _ := e2.Submit(nz, "Movie.2020", "movies", "")
	waitDone(t, e2, j.ID)
	if got := e2.folderName(j.ID); got != "Movie.2020" {
		t.Fatalf("third: %q", got)
	}
	// Renaming gives a new name from the new base.
	if _, err := e2.Rename(second, "Other"); err != nil {
		t.Fatal(err)
	}
	if got := e2.folderName(second); got != "Other" {
		t.Fatalf("renamed: %q", got)
	}
}
