package archive

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"math/rand"
	"testing"
)

// An obfuscated season pack without PAR2: one RAR set per episode, every
// volume named at random. Job.Build puts all of them in one bucket, so
// OpenRAR must keep each set's volumes together and in header order;
// interleaving them by random name served episodes with their middle
// volumes swapped.
func TestObfuscatedVolumesOfSeveralSetsStayApart(t *testing.T) {
	rnd := rand.New(rand.NewSource(7))
	var want []member
	var vols []Volume
	for _, v := range versions() {
		t.Run(fmt.Sprintf("RAR%d", v), func(t *testing.T) {
			want, vols = nil, nil
			for ep := 1; ep <= 3; ep++ {
				data := make([]byte, 250_000)
				rnd.Read(data)
				m := member{Name: fmt.Sprintf("Show.S01E%02d.mkv", ep), Data: data}
				want = append(want, m)
				vols = append(vols, volumesFrom(writeRAR(v, []member{m}, 64_000),
					func(int) string { return fmt.Sprintf("%032x.rar", rnd.Uint64()) })...)
			}
			rnd.Shuffle(len(vols), func(i, j int) { vols[i], vols[j] = vols[j], vols[i] })
			set, err := OpenRAR(context.Background(), vols, 4)
			if err != nil {
				t.Fatal(err)
			}
			if len(set.Members) != len(want) {
				t.Fatalf("%d members, want %d", len(set.Members), len(want))
			}
			byName := map[string][]byte{}
			for _, m := range want {
				byName[m.Name] = m.Data
			}
			for _, m := range set.Members {
				if m.Err != nil {
					t.Fatalf("%s: %v", m.Name, m.Err)
				}
				got := make([]byte, m.Size)
				if n, err := set.ReaderAt(m).ReadAt(got, 0); n != len(got) || (err != nil && err != io.EOF) {
					t.Fatalf("%s: read n=%d err=%v", m.Name, n, err)
				}
				if !bytes.Equal(got, byName[m.Name]) {
					t.Fatalf("%s: bytes differ (volumes out of order)", m.Name)
				}
			}
		})
	}
}
