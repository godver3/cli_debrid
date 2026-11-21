package engine

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

func result(names ...string) *job.Result {
	res := &job.Result{}
	for i, n := range names {
		res.Sources = append(res.Sources, job.Source{NZBIndex: i})
		res.Entries = append(res.Entries, job.Entry{Name: n, Size: 100, Pieces: []job.Piece{{Source: i}}})
	}
	return res
}

func names(res *job.Result) string {
	var out []string
	for _, e := range res.Entries {
		out = append(out, e.Name)
	}
	return strings.Join(out, ",")
}

func TestDeobfuscate(t *testing.T) {
	for _, c := range []struct {
		mode, job string
		in        []string
		want      string
	}{
		{DeobfuscateSeasonEp, "Movie (2024) - 1080p", []string{"a8f3k2j4h5g6d7s8.mkv", "info.nfo"}, "Movie (2024) - 1080p.mkv,info.nfo"},
		{DeobfuscateSeasonEp, "Show.S02.1080p", []string{"x1y2z3a4b5c6d7.mkv", "q1w2e3r4t5y6u7.mkv"}, "S02E01.mkv,S02E02.mkv"},
		{DeobfuscateIndex, "Show.S02.1080p", []string{"x1y2z3a4b5c6d7.mkv", "q1w2e3r4t5y6u7.mkv"}, "Show.S02.1080p - 01.mkv,Show.S02.1080p - 02.mkv"},
		{DeobfuscateSeasonEp, "Show Pack", []string{"x1y2z3a4b5c6d7.mkv", "q1w2e3r4t5y6u7.mkv"}, "Show Pack - 01.mkv,Show Pack - 02.mkv"},
		{DeobfuscateSeasonEp, "Show.S02", []string{"Show.S02E01.mkv", "Show.S02E02.mkv"}, "Show.S02E01.mkv,Show.S02E02.mkv"},
		{"", "Movie", []string{"a8f3k2j4h5g6d7s8.mkv"}, "a8f3k2j4h5g6d7s8.mkv"},
	} {
		res := result(c.in...)
		Deobfuscate(res, c.mode, c.job)
		if got := names(res); got != c.want {
			t.Errorf("%s %q %v: got %s want %s", c.mode, c.job, c.in, got, c.want)
		}
	}
}

func TestExtAllowedAndParsers(t *testing.T) {
	if !extAllowed(nil, "x.srt") || !extAllowed(DefaultAllowedExt, "x.MKV") || extAllowed(DefaultAllowedExt, "x.nfo") {
		t.Fatal("extAllowed")
	}
	if n, _ := ParseSize("1.5GB"); n != 3<<29 {
		t.Fatalf("size %d", n)
	}
	if d, _ := ParseAge("7d"); d != 7*24*time.Hour {
		t.Fatalf("age %v", d)
	}
}

func TestVirtualFoldersAndAllowedExt(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) {
		c.AllowedExt = DefaultAllowedExt
		c.VirtualFolders = map[string]VirtualFolder{
			"shows":   {"regex": `S\d\dE\d\d`},
			"movies":  {"not_regex": `S\d\dE\d\d`, "protocol": "debrid"},
			"__all__": {"include": "x"}, // reserved: skipped
			"broken":  {"regex": "("},   // bad: skipped
		}
	})
	e.AddDebrid("rd-1", "Show.S01E01", "", "rd", []DebridFile{{Name: "e.mkv", Size: 5, Ref: "1:1"}, {Name: "e.nfo", Size: 1, Ref: "1:2"}})
	e.AddDebrid("rd-2", "Film.2024", "", "rd", []DebridFile{{Name: "f.mkv", Size: 5, Ref: "2:1"}})
	e.AddDebrid("rd-3", "Only.Subs", "", "rd", []DebridFile{{Name: "s.srt", Size: 5, Ref: "3:1"}})
	if got := strings.Join(e.TopFolders(), ","); got != "__all__,__bad__,torrents,nzbs,realdebrid,movies,shows" {
		t.Fatalf("tops %s", got)
	}
	list := func(top string) string {
		var out []string
		for _, f := range e.Folders(top) {
			out = append(out, f.Name)
		}
		return strings.Join(out, ",")
	}
	if list("shows") != "Show.S01E01" || list("movies") != "Film.2024" || list("__all__") != "Film.2024,Show.S01E01" {
		t.Fatalf("shows=%q movies=%q all=%q", list("shows"), list("movies"), list("__all__"))
	}
	f, _ := e.Folder(AllFolder, "Show.S01E01")
	if len(f.Entries) != 1 || f.Entries[0].Name != "e.mkv" {
		t.Fatalf("allowed extensions: %+v", f.Entries)
	}
	_ = store.Ready
}

// The default Movies / TV Shows folders cli_debrid writes for new installs
// (utilities/content_routing.py, cli_docs Content routing) must compile in
// RE2 and put every release in exactly one of the two.
func TestDefaultVirtualFoldersPartition(t *testing.T) {
	raw, err := os.ReadFile("testdata/default_virtual_folders.json")
	if err != nil {
		t.Fatal(err)
	}
	var vfs map[string]VirtualFolder
	if err := json.Unmarshal(raw, &vfs); err != nil {
		t.Fatal(err)
	}
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.VirtualFolders = vfs })
	cases := []struct {
		job, file, want string
	}{
		{"Show.Name.S01E05.1080p.WEB-DL", "Show.Name.S01E05.mkv", "TV Shows"},
		{"Show.Name.S02.1080p.BluRay", "a.mkv", "TV Shows"},
		{"Show Name Season 3 Complete", "a.mkv", "TV Shows"},
		{"[Group] Anime Title - 12 [1080p]", "a.mkv", "TV Shows"},
		{"Daily.Show.2024.05.17.720p", "a.mkv", "TV Shows"},
		{"obfuscated8f3k2j", "Show.Name.S03E02.mkv", "TV Shows"},
		{"The.Matrix.1999.1080p.BluRay.x264", "The.Matrix.1999.mkv", "Movies"},
		{"Blade Runner 2049 (2017) 2160p", "movie.mkv", "Movies"},
		{"Se7en.1995.REMASTERED.1080p", "Se7en.mkv", "Movies"},
	}
	for i, c := range cases {
		e.AddDebrid(fmt.Sprintf("rd-%d", i), c.job, "", "rd", []DebridFile{{Name: c.file, Size: 5, Ref: fmt.Sprintf("%d:1", i)}})
	}
	in := func(top, name string) bool {
		for _, f := range e.Folders(top) {
			if f.Name == name {
				return true
			}
		}
		return false
	}
	for _, c := range cases {
		tv, mv := in("TV Shows", c.job), in("Movies", c.job)
		if tv == mv || (c.want == "TV Shows") != tv {
			t.Errorf("%q: tv=%v movies=%v want %s", c.job, tv, mv, c.want)
		}
	}
}

func TestReleaseWithNoAllowedFileFails(t *testing.T) {
	// Grantchester S05E05 4PLAY: only a .ts. Plex can't use it, so the
	// import fails for good and cli_debrid picks another release.
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.AllowedExt = DefaultAllowedExt })
	j, _ := e.Submit(nntptest.PostNZB(s, "g", nntptest.File{Name: "Show.S05E05.ts", Data: data(3, 300_000)}), "Show.S05E05", "tv", "")
	got := waitDone(t, e, j.ID)
	if got.Status != store.Failed || got.Retryable || !strings.Contains(got.Error, "no file with an allowed extension") || !strings.Contains(got.Error, "has ts") {
		t.Fatalf("got %+v", got)
	}
	// A debrid torrent with only hidden files is flagged, not mounted.
	e.AddDebrid("rd-9", "Only.Ts", "", "rd", []DebridFile{{Name: "a.ts", Size: 5, Ref: "9:1"}})
	_, res, _ := e.Store.Get("rd-9")
	if !e.HiddenByExtension(res) || e.FolderPath("rd-9") != "" {
		t.Fatal("debrid job with only .ts should be hidden by extension")
	}
}
