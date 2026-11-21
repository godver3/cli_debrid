package health

import (
	"context"
	"errors"
	"math/rand"
	"sync"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// fakeStat answers from maps: missing ids are proven absent, flaky ids
// fail with a network-style error.
type fakeStat struct {
	missing, flaky map[string]bool
	mu             sync.Mutex
	asked          []string
}

func (f *fakeStat) Stat(ctx context.Context, prio nntp.Priority, id string) (bool, error) {
	f.mu.Lock()
	f.asked = append(f.asked, id)
	f.mu.Unlock()
	if prio != nntp.Prefetch {
		panic("health checks must not compete with playback")
	}
	if f.flaky[id] {
		return false, &nntp.InconclusiveError{ID: id, Last: errors.New("timeout")}
	}
	return !f.missing[id], nil
}

// twoFiles: "movie.mkv" over 100 segments of source 0, "sample.mkv" over
// 3 segments of source 1.
func twoFiles() (*nzb.NZB, *job.Result) {
	mk := func(prefix string, n int) nzb.File {
		f := nzb.File{Name: prefix, Total: n}
		for i := 1; i <= n; i++ {
			f.Segments = append(f.Segments, nzb.Segment{ID: prefix + "." + itoa(i), Number: i})
		}
		return f
	}
	n := &nzb.NZB{Files: []nzb.File{mk("m", 100), mk("s", 3)}}
	res := &job.Result{
		Sources: []job.Source{
			{NZBIndex: 0, Name: "movie.mkv", Layout: stream.Layout{Size: 100 * 1000, PartSize: 1000}},
			{NZBIndex: 1, Name: "sample.mkv", Layout: stream.Layout{Size: 2500, PartSize: 1000}},
		},
		Entries: []job.Entry{
			{Name: "movie.mkv", Size: 100_000, Pieces: []job.Piece{{Source: 0, Offset: 0, Length: 100_000}}},
			{Name: "sample.mkv", Size: 2500, Pieces: []job.Piece{{Source: 1, Offset: 0, Length: 2500}}},
		},
	}
	return n, res
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	s := ""
	for ; i > 0; i /= 10 {
		s = string(rune('0'+i%10)) + s
	}
	return s
}

func opts() Options { return Options{Rand: rand.New(rand.NewSource(1))} }

func TestHealthyJobSamplesFirstLastAndSome(t *testing.T) {
	n, res := twoFiles()
	st := &fakeStat{}
	rep, err := Check(context.Background(), st, n, res, opts())
	if err != nil || rep.Status != Healthy {
		t.Fatalf("%+v %v", rep, err)
	}
	asked := map[string]bool{}
	for _, id := range st.asked {
		asked[id] = true
	}
	if !asked["m.1"] || !asked["m.100"] || !asked["s.1"] || !asked["s.3"] {
		t.Fatalf("first/last not checked: %v", st.asked)
	}
	// 2% of the 98 middle movie segments rounds up to 2; sample's 1 middle segment -> 1.
	if len(st.asked) != 2+2+2+1 {
		t.Fatalf("asked %d: %v", len(st.asked), st.asked)
	}
}

func TestStopsAtFirstConfirmedMiss(t *testing.T) {
	n, res := twoFiles()
	st := &fakeStat{missing: map[string]bool{"m.1": true}}
	rep, _ := Check(context.Background(), st, n, res, Options{Full: true, Parallel: 1})
	if rep.Status != Broken || rep.Files[0].Status != Broken || rep.Files[0].Segment != "m.1" || rep.Files[1].Status != Healthy {
		t.Fatalf("%+v", rep)
	}
	for _, id := range st.asked {
		if id != "m.1" && id[0] == 'm' {
			t.Fatalf("kept checking the movie after a confirmed miss: %v", st.asked)
		}
	}
}

func TestNetworkTroubleIsNeverBroken(t *testing.T) {
	n, res := twoFiles()
	flaky := map[string]bool{}
	for _, f := range n.Files {
		for _, s := range f.Segments {
			flaky[s.ID] = true
		}
	}
	rep, err := Check(context.Background(), &fakeStat{flaky: flaky}, n, res, Options{Full: true})
	if err != nil || rep.Status != Unknown || len(rep.Broken()) != 0 || rep.Inconclusive == 0 {
		t.Fatalf("%+v %v", rep, err)
	}
}

func TestSegmentMissingFromNZBIsBroken(t *testing.T) {
	n, res := twoFiles()
	n.Files[1].Segments = n.Files[1].Segments[:2] // NZB lacks sample segment 3
	rep, _ := Check(context.Background(), &fakeStat{}, n, res, opts())
	if rep.Files[1].Status != Broken || rep.Files[0].Status != Healthy {
		t.Fatalf("%+v", rep)
	}
}

func TestArchiveMemberOnlyChecksItsOwnRange(t *testing.T) {
	n, res := twoFiles()
	// A member using segments 41..60 of source 0 only.
	res.Entries = []job.Entry{{Name: "member.mkv", Size: 20_000, Pieces: []job.Piece{{Source: 0, Offset: 40_000, Length: 20_000}}}}
	st := &fakeStat{missing: map[string]bool{"m.1": true, "m.100": true}} // outside its range
	rep, _ := Check(context.Background(), st, n, res, Options{Full: true, Parallel: 1})
	if rep.Status != Healthy || len(st.asked) != 20 || st.asked[0] != "m.41" || st.asked[1] != "m.60" {
		t.Fatalf("%+v asked %v", rep, st.asked)
	}
}

// slowStat answers after a delay and records how many STATs ran at once.
type slowStat struct {
	fakeStat
	mu2            sync.Mutex
	inFlight, peak int
}

func (f *slowStat) Stat(ctx context.Context, prio nntp.Priority, id string) (bool, error) {
	f.mu2.Lock()
	f.inFlight++
	f.peak = max(f.peak, f.inFlight)
	f.mu2.Unlock()
	defer func() { f.mu2.Lock(); f.inFlight--; f.mu2.Unlock() }()
	select {
	case <-time.After(5 * time.Millisecond):
	case <-ctx.Done():
		return false, ctx.Err()
	}
	return f.fakeStat.Stat(ctx, prio, id)
}

func TestSampleRunsInParallelAndStopsOnMiss(t *testing.T) {
	// Regretting You / Stranger Things: a 10% sample checked one article at
	// a time outran the 5-minute import timeout on a ~1 s/article server.
	n, res := twoFiles()
	st := &slowStat{}
	rep, err := Check(context.Background(), st, n, res, Options{Full: true, Parallel: 8})
	if err != nil || rep.Status != Healthy || rep.Checked != 103 || st.peak < 2 || st.peak > 8 {
		t.Fatalf("%+v err %v peak %d", rep, err, st.peak)
	}
	st = &slowStat{fakeStat: fakeStat{missing: map[string]bool{"m.1": true}}}
	rep, _ = Check(context.Background(), st, n, res, Options{Full: true, Parallel: 8})
	movie := 0
	for _, id := range st.asked {
		if id[0] == 'm' {
			movie++
		}
	}
	if rep.Files[0].Status != Broken || rep.Files[0].Segment != "m.1" || movie >= 100 {
		t.Fatalf("%+v asked %d movie articles", rep, movie)
	}
}
