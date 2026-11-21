package engine

import (
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/job"
)

func entryNames(es []job.Entry) []string {
	out := make([]string, len(es))
	for i, e := range es {
		out[i] = e.Name
	}
	return out
}

func TestMarkExtras(t *testing.T) {
	const gb = int64(1) << 30
	cases := []struct {
		name string
		in   []job.Entry
		want []string
	}{
		{"movie with bonus files", []job.Entry{
			{Name: "Inception.2010.Bluray.1080p-Grym.mkv", Size: 19 * gb},
			{Name: "Inception.Extras1.Jump.Right.into.the.Action-Grym.mkv", Size: 3 * gb},
			{Name: "Inception.Trailer.mkv", Size: gb / 10},
			{Name: "Behind The Scenes.mp4", Size: gb},
			{Name: "Inception.nfo", Size: 100},
		}, []string{"Inception.2010.Bluray.1080p-Grym.mkv", "Inception.Extras1.Jump.Right.into.the.Action-Grym-featurette.mkv",
			"Inception.Trailer-trailer.mkv", "Behind The Scenes-behindthescenes.mp4", "Inception.nfo"}},
		{"small unnamed video in a movie job", []job.Entry{
			{Name: "Movie.2001.1080p.mkv", Size: 10 * gb},
			{Name: "clip01.mkv", Size: gb / 2},
		}, []string{"Movie.2001.1080p.mkv", "clip01-other.mkv"}},
		{"two cuts stay versions", []job.Entry{
			{Name: "Movie.2001.Extended.mkv", Size: 12 * gb},
			{Name: "Movie.2001.Theatrical.mkv", Size: 10 * gb},
		}, []string{"Movie.2001.Extended.mkv", "Movie.2001.Theatrical.mkv"}},
		{"season pack episodes untouched", []job.Entry{
			{Name: "Show.S01E01.mkv", Size: 2 * gb},
			{Name: "Show.S01E02.Behind.the.Scenes.mkv", Size: gb},
			{Name: "Show.S01E03.mkv", Size: gb / 10},
		}, []string{"Show.S01E01.mkv", "Show.S01E02.Behind.the.Scenes.mkv", "Show.S01E03.mkv"}},
		{"word in the title is not an extra", []job.Entry{
			{Name: "Trailer.Park.Boys.The.Movie.2006.mkv", Size: 8 * gb},
			{Name: "Trailer.Park.Boys.Countdown.2009.mkv", Size: 7 * gb},
		}, []string{"Trailer.Park.Boys.The.Movie.2006.mkv", "Trailer.Park.Boys.Countdown.2009.mkv"}},
		{"already suffixed", []job.Entry{
			{Name: "Movie.mkv", Size: 8 * gb},
			{Name: "Making Of-featurette.mkv", Size: gb},
		}, []string{"Movie.mkv", "Making Of-featurette.mkv"}},
		{"single video", []job.Entry{{Name: "Extras.2005.S01.mkv", Size: gb}}, []string{"Extras.2005.S01.mkv"}},
	}
	for _, c := range cases {
		orig := entryNames(c.in)
		got := entryNames(markExtras(c.in))
		if len(got) != len(c.want) {
			t.Fatalf("%s: %v", c.name, got)
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("%s: got %v, want %v", c.name, got, c.want)
				break
			}
		}
		for i, n := range entryNames(c.in) {
			if n != orig[i] {
				t.Errorf("%s: input was modified", c.name)
			}
		}
	}
}

// A renamed extra keeps its real name as a lookup alias, so a symlink made
// against the real name (before the rename, or by cli_mount) still resolves.
func TestMarkExtrasKeepsRealNameForLookups(t *testing.T) {
	const gb = int64(1) << 30
	out := markExtras([]job.Entry{
		{Name: "Movie.2160p.Remux.mkv", Size: 60 * gb},
		{Name: "Movie.1080p.mkv", Size: 8 * gb},
	})
	en := out[1]
	if en.Name != "Movie.1080p-other.mkv" || en.RealName != "Movie.1080p.mkv" {
		t.Fatalf("got %q (real %q)", en.Name, en.RealName)
	}
	if !en.Is("Movie.1080p.mkv") || !en.Is("Movie.1080p-other.mkv") || en.Is("Movie.2160p.Remux.mkv") {
		t.Fatal("Is should match the listed and real names only")
	}
	if out[0].RealName != "" || !out[0].Is("Movie.2160p.Remux.mkv") {
		t.Fatalf("main file changed: %+v", out[0])
	}
}
