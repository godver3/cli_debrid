package job

import (
	"fmt"
	"strings"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// An obfuscated post: most files have no [x/y] counter (subject number 0),
// some do, so nzbparser's (unstable) sort and 0.8.2's stable sort order
// them differently. Files have distinct article counts so a wrong pick
// shows.
func obfuscatedNZB(t *testing.T, files int) *nzb.NZB {
	t.Helper()
	var b strings.Builder
	b.WriteString(`<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">`)
	for i := 0; i < files; i++ {
		subj := fmt.Sprintf("&quot;h%02d.rar&quot; yEnc (1/%d)", i, i+1)
		if i%4 == 0 {
			subj = fmt.Sprintf("[%d/%d] - %s", (i*7)%files+1, files, subj)
		}
		fmt.Fprintf(&b, `<file poster="p" date="1" subject="%s"><groups><group>g</group></groups><segments>`, subj)
		for s := 1; s <= i+1; s++ {
			fmt.Fprintf(&b, `<segment bytes="10" number="%d">f%d.%d@x</segment>`, s, i, s)
		}
		b.WriteString(`</segments></file>`)
	}
	b.WriteString(`</nzb>`)
	n, err := nzb.Parse(strings.NewReader(b.String()))
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// source builds a layout source for file f found at position idx.
func source(f nzb.File, idx int, withFirst bool) Source {
	s := Source{NZBIndex: idx, Name: f.Name, Layout: stream.Layout{Size: int64(len(f.Segments)) * 100, PartSize: 100}}
	if withFirst {
		s.FirstSeg = f.Segments[0].ID
	}
	return s
}

func TestSourceFiles(t *testing.T) {
	n := obfuscatedNZB(t, 30)
	stablePos := map[string]int{} // 0.8.2's positions (nzb tests check StableIndex)
	for i := range n.Files {
		stablePos[n.Files[n.StableIndex(i)].Name] = i
	}
	byName := map[string]int{}
	for i, f := range n.Files {
		byName[f.Name] = i
	}
	check := func(label string, r *Result, want []string) {
		t.Helper()
		got, err := r.SourceFiles(n)
		if err != nil {
			t.Fatalf("%s: %v", label, err)
		}
		for i, w := range want {
			if got[i] == nil || got[i].Name != w {
				t.Fatalf("%s: source %d resolved to %v, want %s", label, i, got[i], w)
			}
		}
	}
	names := []string{"h03.rar", "h17.rar", "h29.rar"}

	// Pre-0.8.2 job: indexes in nzbparser's order.
	r := &Result{}
	for _, nm := range names {
		r.Sources = append(r.Sources, source(n.Files[byName[nm]], byName[nm], false))
	}
	check("legacy", r, names)

	// 0.8.2 job: indexes in the stable order (document order here).
	r = &Result{}
	moved := 0
	for _, nm := range names {
		if stablePos[nm] != byName[nm] {
			moved++
		}
		r.Sources = append(r.Sources, source(n.Files[byName[nm]], stablePos[nm], false))
	}
	if moved == 0 {
		t.Fatal("pick names the two orders place differently")
	}
	check("0.8.2", r, names)

	// New job: the first article decides, even with a wrong index.
	r = &Result{}
	for _, nm := range names {
		r.Sources = append(r.Sources, source(n.Files[byName[nm]], 0, true))
	}
	check("first_seg", r, names)

	// A debrid source has no NZB file.
	r = &Result{Sources: []Source{{NZBIndex: -1, Remote: "rd:x"}, source(n.Files[byName["h05.rar"]], 99, true)}}
	if got, err := r.SourceFiles(n); err != nil || got[0] != nil || got[1].Name != "h05.rar" {
		t.Fatalf("remote: %v %v", got, err)
	}

	// Unknown first article and out-of-range index are errors.
	if _, err := (&Result{Sources: []Source{{FirstSeg: "nope@x"}}}).SourceFiles(n); err == nil {
		t.Fatal("unknown first_seg must error")
	}
	if _, err := (&Result{Sources: []Source{{NZBIndex: 99, Layout: stream.Layout{Size: 1, PartSize: 1}}}}).SourceFiles(n); err == nil {
		t.Fatal("index past the list must error")
	}
}
