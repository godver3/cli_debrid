package archive

import (
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

// make7z builds an archive with the real 7z tool and returns its volumes
// in shuffled order.
func make7z(t *testing.T, level string, split bool, members []member) []Volume {
	t.Helper()
	sevenZ, err := exec.LookPath("7z")
	if err != nil {
		t.Skip("7z not installed")
	}
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	var names []string
	for _, m := range members {
		p := filepath.Join(src, filepath.FromSlash(m.Name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, m.Data, 0o644); err != nil {
			t.Fatal(err)
		}
		names = append(names, filepath.FromSlash(m.Name))
	}
	args := []string{"a", "-mx=" + level, "-mhe=off", "-ms=off"}
	if split {
		args = append(args, "-v64k")
	}
	args = append(args, filepath.Join(dir, "set.7z"))
	args = append(args, names...)
	cmd := exec.Command(sevenZ, args...)
	cmd.Dir = src
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("7z a: %v\n%s", err, b)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "set.7z*"))
	sort.Strings(files)
	var vols []Volume
	for _, f := range files {
		b, _ := os.ReadFile(f)
		vols = append(vols, Volume{Name: filepath.Base(f), R: bytes.NewReader(b), Size: int64(len(b))})
	}
	rand.New(rand.NewSource(5)).Shuffle(len(vols), func(i, j int) { vols[i], vols[j] = vols[j], vols[i] })
	return vols
}

func TestSevenZipStoredSplitStreams(t *testing.T) {
	members := fixtureMembers()
	vols := make7z(t, "0", true, members)
	if len(vols) < 4 {
		t.Fatalf("want a split set, got %d volumes", len(vols))
	}
	set, err := OpenSevenZip(vols)
	if err != nil {
		t.Fatal(err)
	}
	byName := map[string]Member{}
	for _, m := range set.Members {
		byName[m.Name] = m
	}
	for _, want := range members {
		m, ok := byName[want.Name]
		if !ok || m.Err != nil || m.Size != int64(len(want.Data)) {
			t.Fatalf("%s: %+v (members %v)", want.Name, m, set.Members)
		}
		got := make([]byte, m.Size)
		n, err := set.ReaderAt(m).ReadAt(got, 0)
		if n != len(got) || (err != nil && err != io.EOF) || !bytes.Equal(got, want.Data) {
			t.Fatalf("%s: n=%d err=%v data mismatch", m.Name, n, err)
		}
	}
}

func TestSevenZipCompressedIsRefused(t *testing.T) {
	vols := make7z(t, "5", false, []member{{Name: "a.txt", Data: bytes.Repeat([]byte("compress me "), 5000)}})
	set, err := OpenSevenZip(vols)
	if err != nil {
		t.Fatal(err)
	}
	if len(set.Members) != 1 || !errors.Is(set.Members[0].Err, ErrCompressed) {
		t.Fatalf("members %+v", set.Members)
	}
}
