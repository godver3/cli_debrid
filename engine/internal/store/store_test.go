package store

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
)

func TestLifecycleSurvivesReopen(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	clock := time.Unix(1_000, 0)
	s.now = func() time.Time { clock = clock.Add(time.Second); return clock }

	j, err := s.Create(Job{ID: "a1b2-c3", Name: "Show.S01E01", Category: "tv"}, []byte("<nzb/>"))
	if err != nil || j.Status != Queued {
		t.Fatalf("create: %+v %v", j, err)
	}
	if _, err := s.Create(Job{ID: "a1b2-c3"}, nil); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate id: %v", err)
	}
	for _, bad := range []string{"", "../x", "a/b", ".hidden"} {
		if _, err := s.Create(Job{ID: bad}, nil); !errors.Is(err, ErrBadID) {
			t.Fatalf("id %q accepted", bad)
		}
	}
	res := &job.Result{Entries: []job.Entry{{Name: "e.mkv", Size: 100}, {Name: "e.nfo", Size: 5}}}
	if j, err = s.SetResult("a1b2-c3", res); err != nil || j.Status != Ready || j.Size != 105 {
		t.Fatalf("set result: %+v %v", j, err)
	}
	if _, err := s.Update("a1b2-c3", func(j *Job) { j.Tags = []string{"plex"}; j.CLIIDs = map[string]int{"e.mkv": 7} }); err != nil {
		t.Fatal(err)
	}
	// An interrupted import comes back queued.
	if _, err := s.Create(Job{ID: "mid", Name: "M", Status: Importing}, []byte("x")); err != nil {
		t.Fatal(err)
	}

	s2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	got, r, err := s2.Get("a1b2-c3")
	if err != nil || got.Status != Ready || got.CLIIDs["e.mkv"] != 7 || r == nil || len(r.Entries) != 2 {
		t.Fatalf("reopened: %+v %+v %v", got, r, err)
	}
	if b, _ := s2.NZB("a1b2-c3"); string(b) != "<nzb/>" {
		t.Fatalf("nzb %q", b)
	}
	if m, _, _ := s2.Get("mid"); m.Status != Queued {
		t.Fatalf("interrupted import status %q", m.Status)
	}
	if len(s2.List()) != 2 {
		t.Fatalf("list %v", s2.List())
	}
}

func TestChangesAndTombstones(t *testing.T) {
	s, _ := Open(t.TempDir())
	clock := time.Unix(1_000, 0)
	s.now = func() time.Time { return clock }

	s.Create(Job{ID: "old", Name: "Old"}, nil)
	clock = clock.Add(10 * time.Second)
	mark := clock
	clock = clock.Add(time.Second)
	s.Create(Job{ID: "new", Name: "New"}, nil)
	clock = clock.Add(time.Second)
	if err := s.Delete("old"); err != nil {
		t.Fatal(err)
	}
	jobs, gone := s.Changes(mark)
	if len(jobs) != 1 || jobs[0].ID != "new" || len(gone) != 1 || gone[0].ID != "old" {
		t.Fatalf("changes %+v gone %+v", jobs, gone)
	}
	if _, err := os.Stat(filepath.Join(s.dir, "jobs", "old")); !os.IsNotExist(err) {
		t.Fatal("deleted job folder still there")
	}
	// Tombstones persist and expire.
	s2, _ := Open(s.dir)
	s2.now = func() time.Time { return clock.Add(tombstoneKeep + time.Hour) }
	s2.Create(Job{ID: "x"}, nil)
	s2.Delete("x")
	if _, gone := s2.Changes(time.Time{}); len(gone) != 1 || gone[0].ID != "x" {
		t.Fatalf("expired tombstone kept: %+v", gone)
	}
}

// A ready job whose result.json is lost or corrupt stays listed and is
// reported broken (cli_mount's usenet_manifest_invalid), not dropped.
func TestLostResultIsReportedBroken(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	j, err := s.Create(Job{ID: "lost-1", Name: "Movie", CLIIDs: map[string]int{"Movie.mkv": 7}}, []byte("<nzb/>"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Update(j.ID, func(j *Job) { j.Status = Ready }); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "jobs", j.ID, "result.json"), []byte("{trunc"), 0o644); err != nil {
		t.Fatal(err)
	}
	s2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	got, res, err := s2.Get(j.ID)
	if err != nil || res != nil || got.Health == nil || got.Health.Status != "broken" ||
		len(got.Health.Broken) != 1 || got.Health.Broken[0].Name != "Movie.mkv" ||
		got.Health.Broken[0].Reason != "usenet_manifest_invalid" {
		t.Fatalf("%+v %v %v", got.Health, res, err)
	}
}
