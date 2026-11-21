package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

func mapped() *job.Result {
	var segs []stream.MapSeg
	var off int64
	for i := 0; i < 1000; i++ {
		l, d := int64(768_000), int64(0)
		if i == 0 {
			d = 1234 // archive header inside the first article
		}
		if i == 999 {
			l = 1000
		}
		segs = append(segs, stream.MapSeg{Start: off, Len: l, DataStart: d})
		off += l
	}
	return &job.Result{
		Sources: []job.Source{
			{NZBIndex: 0, Name: "a.nfo", Layout: stream.Layout{Size: 10, PartSize: 10}},
			{NZBIndex: 1, Name: "a.mkv", Layout: stream.Layout{Size: off}, Map: segs},
		},
		Entries: []job.Entry{{Name: "a.mkv", Size: off, Pieces: []job.Piece{{Source: 1, Length: off}}}},
	}
}

func TestMapsRoundTrip(t *testing.T) {
	r := mapped()
	b, ok := encodeMaps(r)
	if !ok || len(b) > 64 {
		t.Fatalf("encoded %d bytes (ok=%v), want a few runs", len(b), ok)
	}
	m, err := decodeMaps(b)
	if err != nil || !reflect.DeepEqual(m[1], r.Sources[1].Map) || len(m) != 1 {
		t.Fatalf("decode: %v", err)
	}
	b[len(b)/2] ^= 0xff
	if _, err := decodeMaps(b); err == nil {
		t.Fatal("corrupt maps accepted")
	}
}

func TestMapsLoadOnDemandAndSurviveReopen(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	s.Create(Job{ID: "m1", Name: "Movie"}, []byte("<nzb/>"))
	r := mapped()
	if _, err := s.SetResult("m1", r); err != nil {
		t.Fatal(err)
	}
	if len(r.Sources[1].Map) != 1000 {
		t.Fatal("StoreResult changed the caller's result")
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "jobs", "m1", "result.json"))
	if strings.Contains(string(raw), `"map"`) {
		t.Fatal("result.json still carries the map")
	}
	_, sum, _ := s.Get("m1")
	if len(sum.Sources[1].Map) != 0 || !sum.MapsFile {
		t.Fatal("Get should return the summary")
	}
	s.Close()
	for round := 0; round < 2; round++ { // from folders, then from the index
		s2, err := Open(dir)
		if err != nil {
			t.Fatal(err)
		}
		_, full, err := s2.Full("m1")
		if err != nil || !reflect.DeepEqual(full.Sources[1].Map, r.Sources[1].Map) {
			t.Fatalf("round %d: full: %v", round, err)
		}
		if _, sum, _ := s2.Get("m1"); len(sum.Sources[1].Map) != 0 {
			t.Fatalf("round %d: Full filled the stored summary", round)
		}
		s2.Close()
	}
	// Replacing it with an unmapped result removes maps.bin.
	s3, _ := Open(dir)
	s3.SetResult("m1", &job.Result{Entries: []job.Entry{{Name: "b.mkv", Size: 5}}})
	if _, err := os.Stat(filepath.Join(dir, "jobs", "m1", mapsFile)); !os.IsNotExist(err) {
		t.Fatal("stale maps.bin kept")
	}
	s3.Close()
}

func TestOldInlineMapsAreConverted(t *testing.T) {
	dir := t.TempDir()
	jd := filepath.Join(dir, "jobs", "old1")
	os.MkdirAll(jd, 0o755)
	writeJSON(filepath.Join(jd, "meta.json"), &Job{ID: "old1", Name: "Old", Status: Ready})
	r := mapped()
	b, _ := json.Marshal(r)
	os.WriteFile(filepath.Join(jd, "result.json"), b, 0o644)
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	raw, _ := os.ReadFile(filepath.Join(jd, "result.json"))
	if strings.Contains(string(raw), `"map"`) || !strings.Contains(string(raw), `"maps_file"`) {
		t.Fatalf("not converted: %.200s", raw)
	}
	_, full, err := s.Full("old1")
	if err != nil || !reflect.DeepEqual(full.Sources[1].Map, r.Sources[1].Map) {
		t.Fatalf("full after conversion: %v", err)
	}
}

func TestIndexIgnoresJobsChangedBehindIt(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	s.Create(Job{ID: "k1", Name: "Kept"}, nil)
	s.Create(Job{ID: "k2", Name: "Gone"}, nil)
	s.Close()
	// Changed after the index was saved (e.g. a crash before the next save).
	var j Job
	readJSON(filepath.Join(dir, "jobs", "k1", "meta.json"), &j)
	j.Name = "Renamed outside the index plus some length"
	writeJSON(filepath.Join(dir, "jobs", "k1", "meta.json"), &j)
	os.RemoveAll(filepath.Join(dir, "jobs", "k2"))
	s2, _ := Open(dir)
	defer s2.Close()
	if got, _, _ := s2.Get("k1"); got.Name != j.Name {
		t.Fatalf("stale index entry used: %q", got.Name)
	}
	if _, _, err := s2.Get("k2"); err == nil {
		t.Fatal("deleted job came back from the index")
	}
	// Garbage index: everything is read from the folders.
	os.WriteFile(filepath.Join(dir, indexFile), []byte("junk"), 0o644)
	s3, _ := Open(dir)
	defer s3.Close()
	if got, _, _ := s3.Get("k1"); got.Name != j.Name {
		t.Fatal("garbage index broke loading")
	}
}
