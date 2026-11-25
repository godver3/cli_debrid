package archive

import (
	"bytes"

	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"testing"
)

type member = rartest.Member

func writeRAR(v Version, members []member, volData int) [][]byte {
	return rartest.Write(int(v), members, volData)
}

func fixtureMembers() []member {
	rnd := rand.New(rand.NewSource(42))
	movie := make([]byte, 300_000)
	rnd.Read(movie)
	sample := make([]byte, 20_000)
	rnd.Read(sample)
	return []member{
		{Name: "Some.Movie.2024.1080p.mkv", Data: movie},
		{Name: "Sample/some.movie.sample.mkv", Data: sample},
		{Name: "some.movie.nfo", Data: []byte("release notes\r\n")},
	}
}

func versions() []Version { return []Version{RAR4, RAR5} }

// TestFixturesExtractWith7z proves the test writer emits real RAR: 7-Zip,
// an independent implementation, must extract every member byte-exact
// (it also verifies the CRCs we wrote).
func TestFixturesExtractWith7z(t *testing.T) {
	sevenZ, err := exec.LookPath("7z")
	if err != nil {
		t.Skip("7z not installed")
	}
	members := fixtureMembers()
	for _, v := range versions() {
		t.Run(fmt.Sprintf("RAR%d", v), func(t *testing.T) {
			dir := t.TempDir()
			vols := writeRAR(v, members, 64_000)
			if len(vols) < 4 {
				t.Fatalf("want a multi-volume set, got %d volumes", len(vols))
			}
			for i, b := range vols {
				if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("set.part%02d.rar", i+1)), b, 0o644); err != nil {
					t.Fatal(err)
				}
			}
			out := filepath.Join(dir, "out")
			cmd := exec.Command(sevenZ, "x", "-y", "-o"+out, filepath.Join(dir, "set.part01.rar"))
			if b, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("7z x failed: %v\n%s", err, b)
			}
			for _, m := range members {
				got, err := os.ReadFile(filepath.Join(out, filepath.FromSlash(m.Name)))
				if err != nil || !bytes.Equal(got, m.Data) {
					t.Fatalf("%s: 7z extracted wrong data (err %v)", m.Name, err)
				}
			}
		})
	}
}

func volumesFrom(images [][]byte, names func(i int) string) []Volume {
	vols := make([]Volume, len(images))
	for i, b := range images {
		vols[i] = Volume{Name: names(i), R: bytes.NewReader(b), Size: int64(len(b))}
	}
	return vols
}

func TestStreamMembersFromShuffledObfuscatedVolumes(t *testing.T) {
	members := fixtureMembers()
	for _, v := range versions() {
		t.Run(fmt.Sprintf("RAR%d", v), func(t *testing.T) {
			images := writeRAR(v, members, 64_000)
			vols := volumesFrom(images, func(i int) string { return fmt.Sprintf("x%07d.rar", rand.Intn(1e7)) })
			rand.New(rand.NewSource(3)).Shuffle(len(vols), func(i, j int) { vols[i], vols[j] = vols[j], vols[i] })

			set, err := OpenRAR(context.Background(), vols, 4)
			if err != nil {
				t.Fatal(err)
			}
			if !set.OrderedByHeaders {
				t.Fatal("volumes should be ordered by header volume numbers")
			}
			if len(set.Members) != len(members) {
				t.Fatalf("members %d", len(set.Members))
			}
			rnd := rand.New(rand.NewSource(9))
			for i, m := range set.Members {
				want := members[i]
				if m.Err != nil || m.Name != want.Name || m.Size != int64(len(want.Data)) {
					t.Fatalf("member %d: %+v", i, m)
				}
				r := set.ReaderAt(m)
				all := make([]byte, m.Size)
				if n, err := r.ReadAt(all, 0); n != len(all) || (err != nil && err != io.EOF) || !bytes.Equal(all, want.Data) {
					t.Fatalf("%s: full read n=%d err=%v", m.Name, n, err)
				}
				for k := 0; k < 50 && m.Size > 1; k++ {
					off := rnd.Int63n(m.Size)
					buf := make([]byte, 1+rnd.Intn(150_000))
					n, err := r.ReadAt(buf, off)
					end := min(off+int64(len(buf)), m.Size)
					if int64(n) != end-off || !bytes.Equal(buf[:n], want.Data[off:end]) {
						t.Fatalf("%s: read %d@%d n=%d err=%v", m.Name, len(buf), off, n, err)
					}
					if n < len(buf) && err != io.EOF {
						t.Fatalf("short read must be io.EOF, got %v", err)
					}
				}
			}
		})
	}
}

func TestMissingVolumesMakeMembersIncomplete(t *testing.T) {
	members := fixtureMembers()
	for _, v := range versions() {
		t.Run(fmt.Sprintf("RAR%d", v), func(t *testing.T) {
			images := writeRAR(v, members, 64_000)
			name := func(i int) string { return fmt.Sprintf("set.part%02d.rar", i+1) }

			// Middle volume gone: the movie is short, never served truncated.
			mid := append(append([][]byte{}, images[:2]...), images[3:]...)
			set, err := OpenRAR(context.Background(), volumesFrom(mid, name), 2)
			if err != nil {
				t.Fatal(err)
			}
			if !errors.Is(set.Members[0].Err, ErrIncomplete) {
				t.Fatalf("movie with a volume missing: err %v", set.Members[0].Err)
			}

			// First volume gone (the Silicon Valley case): the movie's start
			// is missing, the small members later in the set still parse.
			set, err = OpenRAR(context.Background(), volumesFrom(images[1:], name), 2)
			if err != nil {
				t.Fatal(err)
			}
			if !errors.Is(set.Members[0].Err, ErrIncomplete) {
				t.Fatalf("movie without its first volume: err %v", set.Members[0].Err)
			}
		})
	}
}

func TestCompressedAndEncryptedMembersAreRefused(t *testing.T) {
	infos := []*VolumeInfo{{Parts: []Part{
		{Name: "a.mkv", Length: 10, Unpacked: 10, Stored: false},
		{Name: "b.mkv", Length: 10, Unpacked: 10, Stored: true, Encrypted: true},
		{Name: "c.mkv", Length: 10, Unpacked: 10, Stored: true},
	}}}
	got := stitch(infos, "")
	if !errors.Is(got[0].Err, ErrCompressed) || !errors.Is(got[1].Err, ErrEncrypted) || got[2].Err != nil {
		t.Fatalf("%v / %v / %v", got[0].Err, got[1].Err, got[2].Err)
	}
}

func TestNotRAR(t *testing.T) {
	if _, err := ParseVolume(bytes.NewReader([]byte("matroska....")), 12); !errors.Is(err, ErrNotRAR) {
		t.Fatalf("got %v", err)
	}
}

// Ported from decypharr rar_volume_order_test.go (same cases).
func TestRAR5MainVolumeNumber(t *testing.T) {
	cases := []struct {
		name string
		data []byte
		want int
	}{
		{"first volume", []byte{rar5MainVolume}, 0},
		{"not multi-volume", []byte{0x00}, 0},
		{"volume 52", []byte{rar5MainVolume | rar5MainVolNum, 51}, 51},
		{"multi-byte vint", []byte{rar5MainVolume | rar5MainVolNum, 0xC8, 0x01}, 200},
		{"truncated number", []byte{rar5MainVolume | rar5MainVolNum}, -1},
		{"empty", nil, -1},
	}
	for _, c := range cases {
		if got := rar5MainVolumeNumber(c.data); got != c.want {
			t.Errorf("%s: got %d, want %d", c.name, got, c.want)
		}
	}
}

// Ported from decypharr rar_volume_order_test.go (same cases).
func TestRAR4EndVolumeNumber(t *testing.T) {
	cases := []struct {
		name  string
		flags uint16
		data  []byte
		want  int
	}{
		{"no volume number", 0x0001, nil, -1},
		{"volume number", rar4EndVolNumber, []byte{0x07, 0x00}, 7},
		{"after data CRC", rar4EndVolNumber | rar4EndDataCRC, []byte{1, 2, 3, 4, 0x2A, 0x01}, 298},
		{"truncated", rar4EndVolNumber, []byte{0x07}, -1},
	}
	for _, c := range cases {
		if got := rar4EndVolumeNumber(c.flags, c.data); got != c.want {
			t.Errorf("%s: got %d, want %d", c.name, got, c.want)
		}
	}
}

func TestVolumeNameOrderFallback(t *testing.T) {
	for _, c := range []struct{ in, want []string }{
		{[]string{"x.part10.rar", "x.part2.rar", "x.part01.rar"}, []string{"x.part01.rar", "x.part2.rar", "x.part10.rar"}},
		{[]string{"x.r01", "x.rar", "x.r00", "x.r10"}, []string{"x.rar", "x.r00", "x.r01", "x.r10"}},
		{[]string{"y.002", "y.000", "y.001"}, []string{"y.000", "y.001", "y.002"}},
	} {
		names := append([]string(nil), c.in...)
		sort.Slice(names, func(i, j int) bool { return volumeNameKey(names[i]) < volumeNameKey(names[j]) })
		for i := range c.want {
			if names[i] != c.want[i] {
				t.Fatalf("got %v, want %v", names, c.want)
			}
		}
	}
}

// Members over 4 GiB (4K remuxes) carry high 32 bits in RAR4's LARGE fields.
func TestRAR4LargeFileSizes(t *testing.T) {
	name := "Big.2160p.mkv"
	hdr := make([]byte, rar4FileFixedSize+8+len(name))
	le := binary.LittleEndian
	le.PutUint32(hdr[7:], 0x00000010)  // pack low
	le.PutUint32(hdr[11:], 0x00000020) // unpacked low
	hdr[25] = rar4MethodStored
	le.PutUint16(hdr[26:], uint16(len(name)))
	le.PutUint32(hdr[rar4FileFixedSize:], 1)   // pack high
	le.PutUint32(hdr[rar4FileFixedSize+4:], 2) // unpacked high
	copy(hdr[rar4FileFixedSize+8:], name)
	p, n, err := parseRAR4File(hdr, rar4LongBlock|rar4FileLarge)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1<<32|0x10 || p.Unpacked != 2<<32|0x20 || p.Name != name || !p.Stored {
		t.Fatalf("pack %d part %+v", n, p)
	}
}
