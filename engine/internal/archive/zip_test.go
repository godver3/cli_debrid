package archive

import (
	"archive/zip"
	"bytes"
	"errors"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"testing"
)

func zipBytes(t *testing.T, files map[string][]byte, method uint16) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	names := make([]string, 0, len(files))
	for n := range files {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		f, err := w.CreateHeader(&zip.FileHeader{Name: n, Method: method})
		if err != nil {
			t.Fatal(err)
		}
		f.Write(files[n])
	}
	w.CreateHeader(&zip.FileHeader{Name: "Sample/"})
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func randBytes(n int, seed int64) []byte {
	b := make([]byte, n)
	rand.New(rand.NewSource(seed)).Read(b)
	return b
}

func vol(name string, b []byte, id int) Volume {
	return Volume{Name: name, R: bytes.NewReader(b), Size: int64(len(b)), ID: id}
}

func readMember(t *testing.T, s *Set, m Member) []byte {
	t.Helper()
	got := make([]byte, m.Size)
	if _, err := s.ReaderAt(m).ReadAt(got, 0); err != nil && !errors.Is(err, io.EOF) {
		t.Fatalf("%s: %v", m.Name, err)
	}
	return got
}

func checkMembers(t *testing.T, s *Set, want map[string][]byte) {
	t.Helper()
	if len(s.Members) != len(want) {
		t.Fatalf("%d members, want %d", len(s.Members), len(want))
	}
	for _, m := range s.Members {
		if m.Err != nil {
			t.Fatalf("%s: %v", m.Name, m.Err)
		}
		if !bytes.Equal(readMember(t, s, m), want[m.Name]) {
			t.Fatalf("%s: bytes differ", m.Name)
		}
	}
}

func TestZipStoredSingleAndSplit(t *testing.T) {
	files := map[string][]byte{"Movie.2020.1080p.mkv": randBytes(300_000, 1), "movie.nfo": []byte("info")}
	z := zipBytes(t, files, zip.Store)
	if !IsZip(z) {
		t.Fatal("not detected")
	}
	s, err := OpenZip([]Volume{vol("x.zip", z, 0)})
	if err != nil {
		t.Fatal(err)
	}
	checkMembers(t, s, files)

	// Byte-split, posted out of order: joined by part number.
	var parts []Volume
	for i, off := 0, 0; off < len(z); i, off = i+1, off+100_000 {
		parts = append(parts, vol("x.zip."+string(rune('0'+(i+1)/100))+string(rune('0'+(i+1)/10%10))+string(rune('0'+(i+1)%10)), z[off:min(off+100_000, len(z))], i))
	}
	parts[0], parts[2] = parts[2], parts[0]
	s, err = OpenZip(parts)
	if err != nil {
		t.Fatal(err)
	}
	checkMembers(t, s, files)
	if len(s.Members[0].Extents) < 2 {
		t.Fatalf("member should span volumes: %+v", s.Members[0].Extents)
	}
}

func TestZipCompressedMemberReported(t *testing.T) {
	z := zipBytes(t, map[string][]byte{"a.mkv": bytes.Repeat([]byte("a"), 10_000)}, zip.Deflate)
	s, err := OpenZip([]Volume{vol("a.zip", z, 0)})
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Members) != 1 || !errors.Is(s.Members[0].Err, ErrCompressed) {
		t.Fatalf("got %+v, want ErrCompressed", s.Members)
	}
}

func TestZipTruncatedSetIsIncomplete(t *testing.T) {
	z := zipBytes(t, map[string][]byte{"a.mkv": randBytes(50_000, 2)}, zip.Store)
	if _, err := OpenZip([]Volume{vol("a.zip.001", z[:30_000], 0)}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("got %v, want ErrIncomplete (no end of central directory)", err)
	}
}

// Spanned sets (.z01, .z02, ..., .zip) made by Info-ZIP's zip -s.
func TestZipSpannedSet(t *testing.T) {
	if _, err := exec.LookPath("zip"); err != nil {
		t.Skip("zip not installed")
	}
	dir := t.TempDir()
	files := map[string][]byte{"Show.S01E01.mkv": randBytes(250_000, 3), "Show.S01E02.mkv": randBytes(180_000, 4)}
	for n, b := range files {
		os.WriteFile(filepath.Join(dir, n), b, 0o644)
	}
	cmd := exec.Command("zip", "-q", "-0", "-s", "64k", "set.zip", "Show.S01E01.mkv", "Show.S01E02.mkv")
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("zip: %v %s", err, out)
	}
	paths, _ := filepath.Glob(filepath.Join(dir, "set.z*"))
	if len(paths) < 3 {
		t.Fatalf("expected a spanned set, got %v", paths)
	}
	var vols []Volume
	for i := len(paths) - 1; i >= 0; i-- { // reversed: order must come from names
		b, _ := os.ReadFile(paths[i])
		vols = append(vols, vol(filepath.Base(paths[i]), b, i))
	}
	s, err := OpenZip(vols)
	if err != nil {
		t.Fatal(err)
	}
	checkMembers(t, s, files)
}
