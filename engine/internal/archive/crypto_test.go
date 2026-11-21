package archive

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
)

const testPassword = "s3cr3t-pässword"

// cryptMembers: sizes that are not whole AES blocks, so padding and the
// last partial block are exercised.
func cryptMembers() []member {
	rnd := rand.New(rand.NewSource(7))
	movie := make([]byte, 300_003)
	rnd.Read(movie)
	sample := make([]byte, 20_001)
	rnd.Read(sample)
	return []member{
		{Name: "Locked.Movie.2024.1080p.mkv", Data: movie},
		{Name: "Sample/locked.sample.mkv", Data: sample},
		{Name: "locked.nfo", Data: []byte("release notes\r\n")},
	}
}

type cryptMode struct {
	name string
	opt  rartest.Options
}

func cryptModes() []cryptMode {
	return []cryptMode{
		{"data", rartest.Options{Password: testPassword}},
		{"headers", rartest.Options{Password: testPassword, EncryptHeaders: true}},
	}
}

func writeEncrypted(t *testing.T, opt rartest.Options) [][]byte {
	t.Helper()
	vols := rartest.WriteOptions(rartest.RAR5, cryptMembers(), 64_000, opt)
	if len(vols) < 4 {
		t.Fatalf("want a multi-volume set, got %d volumes", len(vols))
	}
	return vols
}

// TestEncryptedFixturesExtractWith7z proves the writer's encryption is real
// RAR5 encryption: 7-Zip must extract every member byte-exact with the
// password (it checks the CRCs too) and refuse a wrong one.
func TestEncryptedFixturesExtractWith7z(t *testing.T) {
	sevenZ, err := exec.LookPath("7z")
	if err != nil {
		t.Skip("7z not installed")
	}
	for _, mode := range cryptModes() {
		t.Run(mode.name, func(t *testing.T) {
			dir := t.TempDir()
			for i, b := range writeEncrypted(t, mode.opt) {
				if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("set.part%02d.rar", i+1)), b, 0o644); err != nil {
					t.Fatal(err)
				}
			}
			out := filepath.Join(dir, "out")
			cmd := exec.Command(sevenZ, "x", "-y", "-p"+testPassword, "-o"+out, filepath.Join(dir, "set.part01.rar"))
			if b, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("7z x failed: %v\n%s", err, b)
			}
			for _, m := range cryptMembers() {
				got, err := os.ReadFile(filepath.Join(out, filepath.FromSlash(m.Name)))
				if err != nil || !bytes.Equal(got, m.Data) {
					t.Fatalf("%s: 7z extracted wrong data (err %v)", m.Name, err)
				}
			}
			bad := exec.Command(sevenZ, "x", "-y", "-pnope", "-o"+filepath.Join(dir, "bad"), filepath.Join(dir, "set.part01.rar"))
			if b, err := bad.CombinedOutput(); err == nil {
				t.Fatalf("7z accepted a wrong password:\n%s", b)
			}
		})
	}
}

func TestEncryptedRAR5StreamsFromShuffledVolumes(t *testing.T) {
	for _, mode := range cryptModes() {
		t.Run(mode.name, func(t *testing.T) {
			vols := volumesFrom(writeEncrypted(t, mode.opt), func(i int) string { return fmt.Sprintf("x%07d", i*7919%1000) })
			rand.New(rand.NewSource(3)).Shuffle(len(vols), func(i, j int) { vols[i], vols[j] = vols[j], vols[i] })
			set, err := OpenRARPassword(context.Background(), vols, 4, testPassword)
			if err != nil {
				t.Fatal(err)
			}
			if !set.OrderedByHeaders {
				t.Fatal("volumes should be ordered by header numbers")
			}
			byName := map[string]Member{}
			for _, m := range set.Members {
				byName[m.Name] = m
			}
			for _, want := range cryptMembers() {
				m := byName[want.Name]
				if m.Err != nil || m.Encryption == nil || m.Size != int64(len(want.Data)) {
					t.Fatalf("%s: %+v", want.Name, m)
				}
				r := set.ReaderAt(m)
				got := make([]byte, m.Size)
				if n, err := r.ReadAt(got, 0); n != len(got) || (err != nil && err != io.EOF) || !bytes.Equal(got, want.Data) {
					t.Fatalf("%s: full read n=%d err=%v mismatch", m.Name, n, err)
				}
				rnd := rand.New(rand.NewSource(int64(m.Size)))
				for i := 0; i < 200; i++ {
					off := rnd.Int63n(m.Size)
					n := 1 + rnd.Intn(int(min(m.Size-off, 70_000)))
					buf := make([]byte, n)
					if got, err := r.ReadAt(buf, off); got != n || (err != nil && err != io.EOF) || !bytes.Equal(buf, want.Data[off:off+int64(n)]) {
						t.Fatalf("%s: read %d@%d got %d err %v", m.Name, n, off, got, err)
					}
				}
			}
		})
	}
}

func TestEncryptedRAR5WithoutPassword(t *testing.T) {
	// Data-only encryption lists the files, but each one says a password
	// is needed.
	vols := volumesFrom(writeEncrypted(t, cryptModes()[0].opt), func(i int) string { return fmt.Sprintf("set.part%02d.rar", i+1) })
	set, err := OpenRAR(context.Background(), vols, 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range set.Members {
		if !errors.Is(m.Err, ErrEncrypted) {
			t.Fatalf("%s: want ErrEncrypted, got %v", m.Name, m.Err)
		}
	}
	// Encrypted headers: nothing can be listed.
	vols = volumesFrom(writeEncrypted(t, cryptModes()[1].opt), func(i int) string { return fmt.Sprintf("set.part%02d.rar", i+1) })
	if _, err := OpenRAR(context.Background(), vols, 2); !errors.Is(err, ErrEncrypted) {
		t.Fatalf("want ErrEncrypted, got %v", err)
	}
}

func TestEncryptedRAR5WrongPassword(t *testing.T) {
	names := func(i int) string { return fmt.Sprintf("set.part%02d.rar", i+1) }
	vols := volumesFrom(writeEncrypted(t, cryptModes()[0].opt), names)
	set, err := OpenRARPassword(context.Background(), vols, 2, "wrong")
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range set.Members {
		if !errors.Is(m.Err, ErrWrongPassword) {
			t.Fatalf("%s: want ErrWrongPassword, got %v", m.Name, m.Err)
		}
	}
	vols = volumesFrom(writeEncrypted(t, cryptModes()[1].opt), names)
	if _, err := OpenRARPassword(context.Background(), vols, 2, "wrong"); !errors.Is(err, ErrWrongPassword) {
		t.Fatalf("want ErrWrongPassword, got %v", err)
	}
	// Without a check value a wrong header key shows up as garbage headers.
	vols = volumesFrom(writeEncrypted(t, rartest.Options{Password: testPassword, EncryptHeaders: true, NoPasswordCheck: true}), names)
	if _, err := OpenRARPassword(context.Background(), vols, 2, "wrong"); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("want ErrCorrupt, got %v", err)
	}
	// ...and the right password still opens it.
	if _, err := OpenRARPassword(context.Background(), vols, 2, testPassword); err != nil {
		t.Fatal(err)
	}
}

func TestEncryptedRAR5MissingVolumeIsIncomplete(t *testing.T) {
	images := writeEncrypted(t, cryptModes()[0].opt)
	images = append(images[:1:1], images[2:]...)
	vols := volumesFrom(images, func(i int) string { return fmt.Sprintf("set.part%02d.rar", i+1) })
	set, err := OpenRARPassword(context.Background(), vols, 2, testPassword)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range set.Members {
		if m.Name == "Locked.Movie.2024.1080p.mkv" && !errors.Is(m.Err, ErrIncomplete) {
			t.Fatalf("want ErrIncomplete, got %v", m.Err)
		}
	}
}

// make7zPassword builds a stored, password protected 7z with the real tool.
func make7zPassword(t *testing.T, headers bool, split bool, members []member) []Volume {
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
	he := "-mhe=off"
	if headers {
		he = "-mhe=on"
	}
	args := []string{"a", "-mx=0", he, "-p" + testPassword}
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
	var vols []Volume
	for _, f := range files {
		b, _ := os.ReadFile(f)
		vols = append(vols, Volume{Name: filepath.Base(f), R: bytes.NewReader(b), Size: int64(len(b))})
	}
	rand.New(rand.NewSource(9)).Shuffle(len(vols), func(i, j int) { vols[i], vols[j] = vols[j], vols[i] })
	return vols
}

func TestSevenZipEncryptedStoredStreams(t *testing.T) {
	members := cryptMembers()
	for _, headers := range []bool{false, true} {
		t.Run(fmt.Sprintf("headers=%v", headers), func(t *testing.T) {
			vols := make7zPassword(t, headers, true, members)
			set, err := OpenSevenZipPassword(vols, testPassword)
			if err != nil {
				t.Fatal(err)
			}
			byName := map[string]Member{}
			for _, m := range set.Members {
				byName[m.Name] = m
			}
			for _, want := range members {
				m := byName[want.Name]
				if m.Err != nil || m.Encryption == nil || m.Size != int64(len(want.Data)) {
					t.Fatalf("%s: %+v", want.Name, m)
				}
				r := set.ReaderAt(m)
				got := make([]byte, m.Size)
				if n, err := r.ReadAt(got, 0); n != len(got) || (err != nil && err != io.EOF) || !bytes.Equal(got, want.Data) {
					t.Fatalf("%s: n=%d err=%v mismatch", m.Name, n, err)
				}
				rnd := rand.New(rand.NewSource(int64(m.Size)))
				for i := 0; i < 100; i++ {
					off := rnd.Int63n(m.Size)
					n := 1 + rnd.Intn(int(min(m.Size-off, 50_000)))
					buf := make([]byte, n)
					if got, err := r.ReadAt(buf, off); got != n || (err != nil && err != io.EOF) || !bytes.Equal(buf, want.Data[off:off+int64(n)]) {
						t.Fatalf("%s: read %d@%d got %d err %v", m.Name, n, off, got, err)
					}
				}
			}
		})
	}
}

func TestSevenZipEncryptedWithoutPassword(t *testing.T) {
	members := cryptMembers()
	set, err := OpenSevenZip(make7zPassword(t, false, false, members))
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range set.Members {
		if !errors.Is(m.Err, ErrEncrypted) {
			t.Fatalf("%s: want ErrEncrypted, got %v", m.Name, m.Err)
		}
	}
	vols := make7zPassword(t, true, false, members)
	if _, err := OpenSevenZip(vols); !errors.Is(err, ErrEncrypted) {
		t.Fatalf("want ErrEncrypted, got %v", err)
	}
	if _, err := OpenSevenZipPassword(vols, "wrong"); !errors.Is(err, ErrWrongPassword) {
		t.Fatalf("want ErrWrongPassword, got %v", err)
	}
}
