package nzb

import (
	"strings"
	"testing"
)

const sample = `<?xml version="1.0" encoding="utf-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <head><meta type="title">Some.Movie.2024</meta><meta type="password">pw</meta></head>
 <file poster="p" date="1" subject="Some.Movie [1/2] - &quot;some.movie.mkv&quot; yEnc (1/4)">
  <groups><group>alt.binaries.test</group></groups>
  <segments>
   <segment bytes="100" number="1"> &lt;a1@x&gt; </segment>
   <segment bytes="100" number="2">a2@x</segment>
   <segment bytes="100" number="2">a2dup@x</segment>
   <segment bytes="50" number="4">a4@x</segment>
  </segments>
 </file>
 <file poster="p" date="1" subject="no quotes here">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="10" number="1">b1@x</segment></segments>
 </file>
</nzb>`

func TestParse(t *testing.T) {
	n, err := Parse(strings.NewReader(sample))
	if err != nil {
		t.Fatal(err)
	}
	if n.Meta["password"] != "pw" || len(n.Files) != 2 {
		t.Fatalf("meta %v files %d", n.Meta, len(n.Files))
	}
	f := n.Files[0]
	if f.Name != "some.movie.mkv" {
		t.Fatalf("name %q", f.Name)
	}
	if len(f.Segments) != 3 || f.Segments[0].ID != "a1@x" || f.Segments[1].ID != "a2@x" {
		t.Fatalf("segments %+v", f.Segments)
	}
	if f.Total != 4 || len(f.Missing) != 1 || f.Missing[0] != 3 || f.Complete() {
		t.Fatalf("total %d missing %v", f.Total, f.Missing)
	}
	if n.Files[1].Name != "no quotes here" || !n.Files[1].Complete() {
		t.Fatalf("second file %+v", n.Files[1])
	}
}

func TestParseRejectsEmpty(t *testing.T) {
	if _, err := Parse(strings.NewReader(`<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>`)); err == nil {
		t.Fatal("empty NZB accepted")
	}
}
