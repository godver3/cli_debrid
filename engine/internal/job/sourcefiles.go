package job

import (
	"fmt"

	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

func firstSeg(f nzb.File) string {
	if len(f.Segments) == 0 {
		return ""
	}
	return f.Segments[0].ID
}

// SourceFiles returns the NZB file of each source (nil for debrid ones).
//
// A source with FirstSeg is found by that message-ID. Older sources use
// NZBIndex, a position in nzb.Parse's file list. Engine 0.8.2 ordered that
// list differently (a stable sort); if the older sources don't fit the
// current order (a source needing more or fewer articles than its file
// has) but all fit 0.8.2's, the job was imported by 0.8.2 and that order
// is used.
func (r *Result) SourceFiles(n *nzb.NZB) ([]*nzb.File, error) {
	out := make([]*nzb.File, len(r.Sources))
	var legacy []int
	var byFirst map[string]int
	for i, s := range r.Sources {
		if s.Remote != "" {
			continue
		}
		if s.FirstSeg == "" {
			legacy = append(legacy, i)
			continue
		}
		if s.NZBIndex >= 0 && s.NZBIndex < len(n.Files) && firstSeg(n.Files[s.NZBIndex]) == s.FirstSeg {
			out[i] = &n.Files[s.NZBIndex]
			continue
		}
		if byFirst == nil {
			byFirst = make(map[string]int, len(n.Files))
			for k := range n.Files {
				if id := firstSeg(n.Files[k]); id != "" {
					if _, dup := byFirst[id]; !dup {
						byFirst[id] = k
					}
				}
			}
		}
		k, ok := byFirst[s.FirstSeg]
		if !ok {
			return nil, fmt.Errorf("job: source %q: no NZB file starts with article %s", s.Name, s.FirstSeg)
		}
		out[i] = &n.Files[k]
	}
	if len(legacy) == 0 {
		return out, nil
	}
	pick := func(index func(int) int) ([]*nzb.File, bool) {
		got := make([]*nzb.File, len(legacy))
		fit := true
		for k, i := range legacy {
			s := r.Sources[i]
			idx := index(s.NZBIndex)
			if idx < 0 || idx >= len(n.Files) {
				return nil, false
			}
			got[k] = &n.Files[idx]
			fit = fit && fits(s, got[k])
		}
		return got, fit
	}
	cur, curFit := pick(func(i int) int { return i })
	if !curFit {
		if old, oldFit := pick(n.StableIndex); oldFit {
			cur = old
		}
	}
	if cur == nil {
		s := r.Sources[legacy[0]]
		for _, i := range legacy {
			if r.Sources[i].NZBIndex < 0 || r.Sources[i].NZBIndex >= len(n.Files) {
				s = r.Sources[i]
				break
			}
		}
		return nil, fmt.Errorf("job: source %q points at NZB file %d of %d", s.Name, s.NZBIndex, len(n.Files))
	}
	for k, i := range legacy {
		out[i] = cur[k]
	}
	return out, nil
}

// fits reports whether f has as many articles as source s needs.
func fits(s Source, f *nzb.File) bool {
	switch {
	case len(s.Map) > 0:
		return len(f.Segments) == len(s.Map)
	case s.Layout.PartSize > 0:
		// An NZB may list trailing surplus past the file's end (see
		// stream.Probe); it still fits when it has every needed article.
		need := s.Layout.Parts()
		return need == len(f.Segments) || need == f.Total || (need < f.Total && f.HasPrefix(need))
	}
	return true
}
