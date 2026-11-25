package engine

import (
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

func TestFindFileByName(t *testing.T) {
	e := newEngine(t, nntptest.Start(t), nil)
	add := func(id, name string, files ...DebridFile) {
		t.Helper()
		if _, err := e.AddDebrid(id, name, "tv", "", files); err != nil {
			t.Fatal(err)
		}
	}
	add("realdebrid-A", "Show.S01.1080p", DebridFile{Name: "Show.S01E01.1080p.mkv", Size: 500 << 20, Ref: "A:1"},
		DebridFile{Name: "Show.S01E02.1080p.mkv", Size: 500 << 20, Ref: "A:2"})
	add("realdebrid-B", "Show.S01E03.1080p", DebridFile{Name: "a1b2c3d4.mkv", Size: 500 << 20, Ref: "B:1"},
		DebridFile{Name: "a1b2c3d4.srt", Size: 40 << 10, Ref: "B:2"})
	e.rebuildTree()

	if id, f, ok := e.FindFile([]string{"show.s01e02.1080p.MKV"}); !ok || id != "realdebrid-A" || f != "Show.S01E02.1080p.mkv" {
		t.Fatalf("case-insensitive name: %v %q %q", ok, id, f)
	}
	if id, f, ok := e.FindFile([]string{"Show.S01E03.1080p.mkv"}); !ok || id != "realdebrid-B" || f != "a1b2c3d4.mkv" {
		t.Fatalf("folder named after the file, one video inside: %v %q %q", ok, id, f)
	}
	if id, _, ok := e.FindFile([]string{"Show (2020) - S01E01 - Pilot (Nope.mkv)", "Show.S01E01.1080p.mkv"}); !ok || id != "realdebrid-A" {
		t.Fatal("later names are tried when earlier ones miss")
	}
	if _, _, ok := e.FindFile([]string{"Other.S01E01.mkv", ""}); ok {
		t.Fatal("no match")
	}
}
