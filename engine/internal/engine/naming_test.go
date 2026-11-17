package engine

import (
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
