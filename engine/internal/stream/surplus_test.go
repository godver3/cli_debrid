package stream

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

// withSurplus lists n extra (dead: the server has no such article) segments
// after the file's real ones.
func withSurplus(f nzb.File, n int) nzb.File {
	for i := 0; i < n; i++ {
		f.Segments = append(f.Segments, nzb.Segment{ID: fmt.Sprintf("dead%d@x", i), Number: f.Total + 1 + i, Bytes: 1000})
	}
	f.Total += n
	return f
}

func TestTrailingSurplusIsIgnored(t *testing.T) {
	for _, tc := range []struct {
		name       string
		size, part int
		surplus    int
	}{
		{"one dead extra", 2_100_000, 700_000, 1},
		{"several dead extras", 2_500_000, 700_000, 5},
		{"single part file", 500_000, 700_000, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			data, _, s, c, f := setup(t, tc.size, tc.part)
			real := f.Total
			f = withSurplus(f, tc.surplus)
			cache := NewCache(64 << 20)
			l, err := Probe(context.Background(), c, cache, f)
			if err != nil {
				t.Fatalf("surplus must be tolerated: %v", err)
			}
			if l.Parts() != real {
				t.Fatalf("parts %d, want %d", l.Parts(), real)
			}
			r := Open(c, cache, f, l, ReadAhead{Segments: 8})
			got := make([]byte, len(data))
			if n, err := r.ReadAt(got, 0); n != len(data) || (err != nil && err.Error() != "EOF") || !bytes.Equal(got, data) {
				t.Fatalf("read %d/%d: %v", n, len(data), err)
			}
			if _, ok := r.byNumber[real+1]; ok {
				t.Fatal("surplus segment kept in the file's segment table")
			}
			for _, id := range s.BodyOrder() {
				if len(id) >= 4 && id[:4] == "dead" {
					t.Fatalf("surplus article %s was fetched", id)
				}
			}
		})
	}
}

func TestSurplusDoesNotHideShortfallOrGaps(t *testing.T) {
	// shortfall: the NZB lacks the tail
	_, _, _, c, f := setup(t, 2_100_000, 700_000)
	f.Segments, f.Total = f.Segments[:2], 2
	if _, err := Probe(context.Background(), c, NewCache(64<<20), f); !errors.Is(err, ErrLayout) {
		t.Fatalf("shortfall: got %v, want ErrLayout", err)
	}
	// gap inside the needed range, with surplus on top
	_, _, _, c, f = setup(t, 2_100_000, 700_000)
	f = withSurplus(f, 2)
	f.Segments = append(f.Segments[:1], f.Segments[2:]...)
	f.Missing = []int{2}
	if _, err := Probe(context.Background(), c, NewCache(64<<20), f); !errors.Is(err, ErrLayout) {
		t.Fatalf("gap: got %v, want ErrLayout", err)
	}
	// gap only inside the surplus is fine
	_, _, _, c, f = setup(t, 2_100_000, 700_000)
	f = withSurplus(f, 3)
	f.Segments = append(f.Segments[:4], f.Segments[5:]...) // drops number 5
	f.Missing = []int{5}
	if _, err := Probe(context.Background(), c, NewCache(64<<20), f); err != nil {
		t.Fatalf("gap in surplus only: %v", err)
	}
	// surplus never turns a missing needed article into anything but proof
	_, _, _, c, f = setup(t, 2_100_000, 700_000)
	f = withSurplus(f, 2)
	f.Segments[1].ID = "gone@x" // a needed article that no provider has
	cache := NewCache(64 << 20)
	l, err := Probe(context.Background(), c, cache, f)
	if err != nil {
		t.Fatal(err)
	}
	_, err = Open(c, cache, f, l, ReadAhead{}).ReadAt(make([]byte, 10), 800_000)
	if !nntp.IsNotFound(err) {
		t.Fatalf("got %v, want not-found proof on the needed article", err)
	}
}
