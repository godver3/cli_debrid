package nzb

import (
	"strings"
	"testing"
)

// Found live 2026-11-21: posters reused a message-ID at two segment numbers
// (Blue Lights S01E02: 53 times; an Austin City Limits episode: every ID
// twice). The library parser dropped the repeats, the map positions they
// held had no article, and playback marked both playable files broken.
const reused = `<?xml version="1.0" encoding="utf-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p" date="1" subject="&quot;show.mkv&quot; yEnc (1/4)">
  <groups><group>alt.binaries.test</group></groups>
  <segments>
   <segment bytes="100" number="1">a@x</segment>
   <segment bytes="100" number="2">b@x</segment>
   <segment bytes="100" number="3">a@x</segment>
   <segment bytes="100" number="4">c@x</segment>
  </segments>
 </file>
 <file poster="p" date="1" subject="&quot;show.mkv&quot; yEnc (1/4)">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="100" number="4">c-repost@x</segment></segments>
 </file>
</nzb>`

func TestReusedMessageIDKeepsEveryNumber(t *testing.T) {
	n, err := Parse(strings.NewReader(reused))
	if err != nil {
		t.Fatal(err)
	}
	if len(n.Files) != 1 {
		t.Fatalf("same-subject <file> entries must merge, got %d files", len(n.Files))
	}
	f := n.Files[0]
	want := []string{"a@x", "b@x", "a@x", "c@x"} // the repeated number 4 keeps its first ID
	if len(f.Segments) != len(want) || len(f.Missing) != 0 || f.Total != 4 {
		t.Fatalf("segments %+v missing %v total %d", f.Segments, f.Missing, f.Total)
	}
	for i, s := range f.Segments {
		if s.Number != i+1 || s.ID != want[i] {
			t.Fatalf("segment %d = %+v, want number %d id %s", i, s, i+1, want[i])
		}
	}
}
