package mount

import (
	"bytes"
	"errors"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"syscall"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

func random(seed int64, n int) []byte {
	b := make([]byte, n)
	rand.New(rand.NewSource(seed)).Read(b)
	return b
}

func fuseAvailable(t *testing.T) {
	t.Helper()
	if _, err := os.Stat("/dev/fuse"); err != nil {
		t.Skip("no /dev/fuse")
	}
	if _, err := exec.LookPath("fusermount3"); err != nil {
		if _, err := exec.LookPath("fusermount"); err != nil {
			t.Skip("no fusermount")
		}
	}
}

func waitReady(t *testing.T, e *engine.Engine, id string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		j, _, _ := e.Store.Get(id)
		if j.Status == store.Ready {
			return
		}
		if j.Status == store.Failed {
			t.Fatalf("import failed: %s", j.Error)
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("import timed out")
}

func names(t *testing.T, dir string) []string {
	t.Helper()
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, e := range ents {
		out = append(out, e.Name())
	}
	sort.Strings(out)
	return out
}

func TestMountServesJobsThroughFUSE(t *testing.T) {
	fuseAvailable(t)
	s := nntptest.Start(t)
	host, port := s.Addr()
	e, err := engine.New(engine.Config{
		DataDir: t.TempDir(), MountPath: "unused",
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 8, Timeout: 5 * time.Second}},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()

	movie := random(1, 500_000)
	j, err := e.Submit(nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.2024.mkv", Data: movie}), "Movie.2024", "movies", "")
	if err != nil {
		t.Fatal(err)
	}
	waitReady(t, e, j.ID)

	dir := t.TempDir()
	srv, err := Mount(e, dir, Options{UID: uint32(os.Getuid()), GID: uint32(os.Getgid())}, nil)
	if err != nil {
		t.Skipf("mount not permitted here: %v", err)
	}
	defer srv.Unmount()

	if got := names(t, dir); len(got) != 2 || got[0] != "__all__" || got[1] != "nzbs" {
		t.Fatalf("root %v", got)
	}
	path := filepath.Join(dir, "__all__", "Movie.2024", "Movie.2024.mkv")
	got, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(got, movie) {
		t.Fatalf("full read through FUSE: err %v, equal %v", err, bytes.Equal(got, movie))
	}
	if same, _ := os.ReadFile(filepath.Join(dir, "nzbs", "Movie.2024", "Movie.2024.mkv")); !bytes.Equal(same, movie) {
		t.Fatal("nzbs/ view differs")
	}
	f, _ := os.Open(path)
	rnd := rand.New(rand.NewSource(2))
	for i := 0; i < 40; i++ {
		off := rnd.Int63n(int64(len(movie)))
		buf := make([]byte, 1+rnd.Intn(200_000))
		n, err := f.ReadAt(buf, off)
		end := min(off+int64(len(buf)), int64(len(movie)))
		if int64(n) != end-off || !bytes.Equal(buf[:n], movie[off:end]) || (err != nil && err != io.EOF) {
			t.Fatalf("ReadAt %d@%d: n=%d err=%v", len(buf), off, n, err)
		}
	}
	f.Close()
	if st, _ := os.Stat(path); st.Size() != int64(len(movie)) || st.Mode().Perm() != 0o444 {
		t.Fatalf("stat %v %v", st.Size(), st.Mode())
	}
	if err := os.WriteFile(path, []byte("x"), 0o644); err == nil {
		t.Fatal("write succeeded on a read-only mount")
	}

	// A RAR set imported after mounting shows up without a remount.
	ep := random(3, 300_000)
	vols := rartest.Write(rartest.RAR4, []rartest.Member{{Name: "Show.S01E01.mkv", Data: ep}}, 80_000)
	var files []nntptest.File
	for i, v := range vols {
		files = append(files, nntptest.File{Name: "show.s01e01.part0" + string(rune('1'+i)) + ".rar", Data: v})
	}
	j2, err := e.Submit(nntptest.PostNZB(s, "r", files...), "Show.S01E01", "tv", "")
	if err != nil {
		t.Fatal(err)
	}
	waitReady(t, e, j2.ID)
	time.Sleep(dirTTL + 200*time.Millisecond)
	if got := names(t, filepath.Join(dir, "__all__")); len(got) != 2 {
		t.Fatalf("after second import: %v", got)
	}
	if got, err := os.ReadFile(filepath.Join(dir, "__all__", "Show.S01E01", "Show.S01E01.mkv")); err != nil || !bytes.Equal(got, ep) {
		t.Fatalf("RAR member through FUSE: %v", err)
	}

	// Playback hitting an article missing everywhere marks the file broken
	// for cli_debrid's repair (the read itself fails with EIO).
	gone := random(4, 300_000)
	j3, err := e.Submit(nntptest.PostNZB(s, "g", nntptest.File{Name: "Gone.mkv", Data: gone}), "Gone", "movies", "")
	if err != nil {
		t.Fatal(err)
	}
	waitReady(t, e, j3.ID)
	s.Remove("g0.3@test")
	time.Sleep(dirTTL + 200*time.Millisecond)
	if _, err := os.ReadFile(filepath.Join(dir, "__all__", "Gone", "Gone.mkv")); err == nil {
		t.Fatal("read of a file with a missing article succeeded")
	}
	if g, _, _ := e.Store.Get(j3.ID); g.Health == nil || g.Health.Status != "broken" ||
		len(g.Health.Broken) != 1 || g.Health.Broken[0].Via != "playback" || g.Health.Broken[0].Segment != "g0.3@test" {
		t.Fatalf("playback miss not recorded: %+v", g.Health)
	}

	// Deleted jobs disappear.
	if err := e.Delete(j.ID); err != nil {
		t.Fatal(err)
	}
	time.Sleep(dirTTL + 200*time.Millisecond)
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) && !errors.Is(err, syscall.ENOENT) {
		t.Fatalf("deleted job still visible: %v", err)
	}
}
