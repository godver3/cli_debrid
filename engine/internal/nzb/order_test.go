package nzb

import (
	"encoding/xml"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/Tensai75/nzbparser"
)

// Jobs store their sources as positions in Parse's file list, so Parse
// must order files exactly as nzbparser.Parse did when they were imported.
// 0.8.2 used a stable sort; obfuscated posts (many files with the same
// subject number) came out in another order and old jobs read the wrong
// files.
func TestFileOrderMatchesNzbparser(t *testing.T) {
	for _, n := range []int{5, 13, 40, 95} {
		doc := mixedNZB(n)
		ours, err := Parse(strings.NewReader(doc))
		if err != nil {
			t.Fatal(err)
		}
		lib, err := nzbparser.ParseString(doc)
		if err != nil {
			t.Fatal(err)
		}
		if len(ours.Files) != len(lib.Files) {
			t.Fatalf("n=%d: %d files, nzbparser %d", n, len(ours.Files), len(lib.Files))
		}
		for i := range lib.Files {
			if ours.Files[i].Subject != lib.Files[i].Subject {
				t.Fatalf("n=%d: file %d is %q, nzbparser has %q", n, i, ours.Files[i].Subject, lib.Files[i].Subject)
			}
		}
	}
}

// StableIndex must be exactly 0.8.2's order: its parseRaw with sort.Stable.
func TestStableIndexIs082Order(t *testing.T) {
	for _, n := range []int{13, 40, 95} {
		doc := mixedNZB(n)
		got, err := Parse(strings.NewReader(doc))
		if err != nil {
			t.Fatal(err)
		}
		// 0.8.2: decode, merge by subject, scan, sort.Stable, drop empty files.
		x := new(rawNZB)
		if err := xml.Unmarshal([]byte(doc), x); err != nil {
			t.Fatal(err)
		}
		old := &nzbparser.Nzb{}
		at := map[string]int{}
		for _, f := range x.Files {
			if i, ok := at[f.Subject]; ok {
				old.Files[i].Segments = append(old.Files[i].Segments, f.Segments...)
				continue
			}
			at[f.Subject] = len(old.Files)
			old.Files = append(old.Files, f)
		}
		nzbparser.ScanNzbFile(old)
		sort.Stable(old.Files)
		differs := false
		for i, f := range old.Files {
			k := got.StableIndex(i)
			if k < 0 || got.Files[k].Subject != f.Subject {
				t.Fatalf("n=%d: stable position %d is file %d, 0.8.2 had %q", n, i, k, f.Subject)
			}
			differs = differs || k != i
		}
		if !differs {
			t.Fatalf("n=%d: test NZB must order differently under the two sorts", n)
		}
	}
}

// mixedNZB: mostly unnumbered subjects (number 0), some "[x/y]", out of
// order, as obfuscated posts look.
func mixedNZB(n int) string {
	var b strings.Builder
	b.WriteString(`<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">`)
	for i := 0; i < n; i++ {
		// Mostly no [x/y] counter (all number 0), some numbered, out of order.
		subj := fmt.Sprintf("&quot;h%02d.part%03d.rar&quot; yEnc (1/%d)", (i*37)%n, (i*53)%n, 3+i%5)
		if i%7 == 0 {
			subj = fmt.Sprintf("[%d/%d] - %s", (i*11)%n+1, n, subj)
		}
		fmt.Fprintf(&b, `<file poster="p" date="1" subject="%s"><groups><group>g</group></groups><segments>`, subj)
		for s := 1; s <= 1+i%4; s++ {
			fmt.Fprintf(&b, `<segment bytes="10" number="%d">f%d.s%d@x</segment>`, s, i, s)
		}
		b.WriteString(`</segments></file>`)
	}
	b.WriteString(`</nzb>`)
	return b.String()
}
