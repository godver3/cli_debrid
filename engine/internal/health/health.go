// Package health checks whether a job's articles still exist, for
// cli_debrid's repair system. It samples articles with STAT (no download),
// always including each file's first and last article, and stops on a file
// at its first confirmed missing article.
//
// Only proof marks a file broken: an article every provider answered
// "no such article" for, or one the NZB never listed. Timeouts, dropped
// connections and auth failures are counted as inconclusive and never
// turn a file broken.
package health

import (
	"context"
	"fmt"
	"math/rand"
	"sort"
	"sync"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

// Stater checks article existence. *nntp.Client implements it.
type Stater interface {
	Stat(ctx context.Context, prio nntp.Priority, id string) (bool, error)
}

// Options tune a check.
type Options struct {
	// SampleRate is the share of each file's articles checked beyond the
	// first and last (default 0.02); Max caps that per file (default 30).
	// Full checks every article.
	SampleRate float64
	Max        int
	Full       bool
	Rand       *rand.Rand // for tests; random sampling otherwise
	// Parallel is how many STATs run at once per file (default 16). One at
	// a time, a 10% sample of a 7 GB release (~1000 articles at ~1 s each
	// on a slow server) outran the 5-minute import timeout.
	Parallel int
}

// Result statuses.
const (
	Healthy = "healthy"
	Broken  = "broken"
	Unknown = "unknown" // nothing could be checked conclusively
)

// FileResult is one entry's outcome.
type FileResult struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
	Segment string `json:"segment,omitempty"` // message-id proven missing
}

// Report is a job's outcome.
type Report struct {
	Status       string       `json:"status"`
	Files        []FileResult `json:"files"`
	Checked      int          `json:"checked"`      // conclusive answers
	Inconclusive int          `json:"inconclusive"` // network trouble etc.
}

// Broken returns the files proven broken.
func (r Report) Broken() []FileResult {
	var out []FileResult
	for _, f := range r.Files {
		if f.Status == Broken {
			out = append(out, f)
		}
	}
	return out
}

type probe struct {
	source int
	number int // 1-based segment number in the source's NZB file
}

// Check samples every entry of a built job.
func Check(ctx context.Context, c Stater, n *nzb.NZB, res *job.Result, opt Options) (Report, error) {
	if opt.SampleRate <= 0 {
		opt.SampleRate = 0.02
	}
	if opt.Max <= 0 {
		opt.Max = 30
	}
	if opt.Rand == nil {
		opt.Rand = rand.New(rand.NewSource(rand.Int63()))
	}
	// Segment id by (source, number); absent = not listed in the NZB.
	ids := make([]map[int]string, len(res.Sources))
	for i, s := range res.Sources {
		if s.NZBIndex < 0 || s.NZBIndex >= len(n.Files) {
			return Report{}, fmt.Errorf("health: source %q points at NZB file %d", s.Name, s.NZBIndex)
		}
		ids[i] = map[int]string{}
		for _, seg := range n.Files[s.NZBIndex].Segments {
			ids[i][seg.Number] = seg.ID
		}
	}
	if opt.Parallel <= 0 {
		opt.Parallel = 16
	}
	rep := Report{}
	known := map[string]bool{} // article id -> exists (shared by entries)
	for _, e := range res.Entries {
		if err := ctx.Err(); err != nil {
			return rep, err
		}
		fr := FileResult{Name: e.Name, Status: Healthy}
		probes := sample(res, e, opt)
		for _, p := range probes {
			if _, listed := ids[p.source][p.number]; !listed {
				fr.Status, fr.Reason = Broken, fmt.Sprintf("segment %d of %s not listed in the NZB", p.number, res.Sources[p.source].Name)
				break
			}
		}
		if fr.Status == Healthy {
			conclusive, err := checkProbes(ctx, c, probes, ids, res, known, &rep, &fr, opt.Parallel)
			if err != nil {
				return rep, err
			}
			if fr.Status == Healthy && conclusive == 0 {
				fr.Status = Unknown
			}
		}
		rep.Files = append(rep.Files, fr)
	}
	rep.Status = Healthy
	anyKnown := false
	for _, f := range rep.Files {
		if f.Status == Broken {
			rep.Status = Broken
		}
		if f.Status != Unknown {
			anyKnown = true
		}
	}
	if rep.Status != Broken && !anyKnown {
		rep.Status = Unknown
	}
	return rep, nil
}

// checkProbes STATs one entry's probes, parallel ones at a time, and stops
// at the first article proven missing (marking fr broken). It returns how
// many probes got a conclusive answer; an error only when ctx ended.
func checkProbes(ctx context.Context, c Stater, probes []probe, ids []map[int]string, res *job.Result,
	known map[string]bool, rep *Report, fr *FileResult, parallel int) (int, error) {
	run, stop := context.WithCancel(ctx)
	defer stop()
	var (
		mu         sync.Mutex
		wg         sync.WaitGroup
		conclusive int
		next       int
	)
	take := func() (probe, bool) {
		mu.Lock()
		defer mu.Unlock()
		if next >= len(probes) || fr.Status == Broken {
			return probe{}, false
		}
		next++
		return probes[next-1], true
	}
	for w := 0; w < min(parallel, len(probes)); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				p, ok := take()
				if !ok || run.Err() != nil {
					return
				}
				id := ids[p.source][p.number]
				mu.Lock()
				exists, seen := known[id]
				mu.Unlock()
				if !seen {
					var err error
					exists, err = c.Stat(run, nntp.Prefetch, id)
					if err != nil {
						if run.Err() != nil {
							return // missing found elsewhere, or ctx ended
						}
						mu.Lock()
						rep.Inconclusive++
						mu.Unlock()
						continue
					}
					mu.Lock()
					known[id] = exists
					rep.Checked++
					mu.Unlock()
				}
				mu.Lock()
				conclusive++
				if !exists && fr.Status != Broken {
					fr.Status, fr.Segment = Broken, id
					fr.Reason = fmt.Sprintf("article missing on every provider (segment %d of %s)", p.number, res.Sources[p.source].Name)
					stop()
				}
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		return conclusive, err
	}
	return conclusive, nil
}

// sample picks which segments to check for an entry: first and last of
// each piece, then random ones (or all of them for a full check).
func sample(res *job.Result, e job.Entry, opt Options) []probe {
	var must, rest []probe
	for _, pc := range e.Pieces {
		if pc.Length <= 0 {
			continue
		}
		src := res.Sources[pc.Source]
		var first, last int
		if len(src.Map) > 0 { // mapped (migrated from cli_mount): look the segments up
			first = src.MapIndex(pc.Offset) + 1
			last = src.MapIndex(pc.Offset+pc.Length-1) + 1
		} else {
			l := src.Layout
			if l.PartSize <= 0 {
				continue
			}
			first = int(pc.Offset/l.PartSize) + 1
			last = int((pc.Offset+pc.Length-1)/l.PartSize) + 1
		}
		must = append(must, probe{pc.Source, first})
		if last != first {
			must = append(must, probe{pc.Source, last})
		}
		for n := first + 1; n < last; n++ {
			rest = append(rest, probe{pc.Source, n})
		}
	}
	if opt.Full {
		return append(must, rest...)
	}
	k := int(float64(len(rest))*opt.SampleRate + 0.999)
	k = min(k, opt.Max, len(rest))
	opt.Rand.Shuffle(len(rest), func(i, j int) { rest[i], rest[j] = rest[j], rest[i] })
	picked := rest[:k]
	sort.Slice(picked, func(i, j int) bool {
		if picked[i].source != picked[j].source {
			return picked[i].source < picked[j].source
		}
		return picked[i].number < picked[j].number
	})
	return append(must, picked...)
}
