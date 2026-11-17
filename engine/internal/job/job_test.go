package job

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/par2"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

type env struct {
	t     *testing.T
	s     *nntptest.Server
	c     *nntp.Client
	nzb   *nzb.NZB
	seq   int
	cache *stream.Cache
}

func newEnv(t *testing.T) *env {
	s := nntptest.Start(t)
	host, port := s.Addr()
	c, err := nntp.NewClient([]nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 8, Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return &env{t: t, s: s, c: c, nzb: &nzb.NZB{}, cache: stream.NewCache(256 << 20)}
}

// post uploads data under subject (what the NZB says) and yencName (what
// the article headers say), and adds it to the NZB.
func (e *env) post(subject, yencName string, data []byte) nzb.File {
	e.seq++
	arts := nntptest.Post(fmt.Sprintf("f%d", e.seq), yencName, data, 64_000)
	e.s.Add(arts...)
	f := nzb.File{Name: subject, Subject: subject, Total: len(arts)}
	for i, a := range arts {
		f.Segments = append(f.Segments, nzb.Segment{ID: a.ID, Number: i + 1, Bytes: len(a.Body)})
	}
	e.nzb.Files = append(e.nzb.Files, f)
	return f
}

func (e *env) build() (*Result, error) {
	return Build(context.Background(), e.c, e.cache, e.nzb, Options{Parallel: 4})
}

// read returns an entry's full content through job.Open, as the mount will.
func (e *env) read(res *Result, entry Entry) []byte {
	fs, err := Open(res, e.nzb, e.c, stream.NewCache(64<<20), stream.ReadAhead{Segments: 2})
	if err != nil {
		e.t.Fatal(err)
	}
	buf := make([]byte, entry.Size)
	if n, err := fs.Entry(entry).ReadAt(buf, 0); int64(n) != entry.Size || (err != nil && err != io.EOF) {
		e.t.Fatalf("%s: read %d/%d: %v", entry.Name, n, entry.Size, err)
	}
	return buf
}

func random(seed int64, n int) []byte {
	b := make([]byte, n)
	rand.New(rand.NewSource(seed)).Read(b)
	copy(b, []byte{0x1A, 0x45, 0xDF, 0xA3}) // a real MKV start: archive members are header-checked
	return b
}

func par2Index(files map[string][]byte) []byte {
	names := make([]string, 0, len(files))
	for n := range files {
		names = append(names, n)
	}
	sort.Strings(names)
	var out []byte
	for i, name := range names {
		data := files[name]
		body := make([]byte, 56)
		body[0] = byte(i + 1)
		h := par2.Hash16k(data)
		copy(body[32:], h[:])
		binary.LittleEndian.PutUint64(body[48:], uint64(len(data)))
		body = append(body, name...)
		for len(body)%4 != 0 {
			body = append(body, 0)
		}
		p := make([]byte, 64, 64+len(body))
		copy(p, "PAR2\x00PKT")
		binary.LittleEndian.PutUint64(p[8:], uint64(64+len(body)))
		copy(p[48:], "PAR 2.0\x00FileDesc")
		p = append(p, body...)
		sum := md5.Sum(p[32:])
		copy(p[16:32], sum[:])
		out = append(out, p...)
	}
	return out
}

func entryNames(res *Result) []string {
	var n []string
	for _, e := range res.Entries {
		n = append(n, e.Name)
	}
	return n
}

func TestPlainFilesAndPar2(t *testing.T) {
	e := newEnv(t)
	movie := random(1, 300_000)
	e.post("Movie.2024.mkv", "Movie.2024.mkv", movie)
	e.post("Movie.2024.nfo", "Movie.2024.nfo", []byte("notes"))
	e.post("Movie.2024.par2", "Movie.2024.par2", par2Index(map[string][]byte{"Movie.2024.mkv": movie}))
	vol := e.post("Movie.2024.vol00+01.par2", "Movie.2024.vol00+01.par2", random(2, 100_000))

	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	if got := entryNames(res); len(got) != 2 || got[0] != "Movie.2024.mkv" || got[1] != "Movie.2024.nfo" {
		t.Fatalf("entries %v", got)
	}
	if !bytes.Equal(e.read(res, res.Entries[0]), movie) {
		t.Fatal("movie bytes differ")
	}
	for _, id := range e.s.BodyOrder() {
		if id == vol.Segments[0].ID {
			t.Fatal("a PAR2 recovery volume was downloaded")
		}
	}
}

func rarSet(t *testing.T) ([]rartest.Member, [][]byte) {
	members := []rartest.Member{
		{Name: "Show.S01E01.1080p.mkv", Data: random(3, 400_000)},
		{Name: "Sample/show.s01e01.sample.mkv", Data: random(4, 30_000)},
	}
	return members, rartest.Write(rartest.RAR5, members, 90_000)
}

func TestObfuscatedRARSetNamedByPar2(t *testing.T) {
	e := newEnv(t)
	members, vols := rarSet(t)
	real := map[string][]byte{}
	order := rand.New(rand.NewSource(8)).Perm(len(vols)) // posted out of order
	for _, i := range order {
		name := fmt.Sprintf("show.s01e01.part%02d.rar", i+1)
		real[name] = vols[i]
		token := fmt.Sprintf("Xk%dQz%07d", i, rand.Intn(1e7))
		e.post(token, token, vols[i]) // subject and yEnc name both obfuscated
	}
	e.post("a8f3e0c1d2b4", "a8f3e0c1d2b4", par2Index(real))

	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	if got := entryNames(res); len(got) != 2 || got[0] != "Show.S01E01.1080p.mkv" || got[1] != "show.s01e01.sample.mkv" {
		t.Fatalf("entries %v (failures %v)", got, res.Failures)
	}
	if !bytes.Equal(e.read(res, res.Entries[0]), members[0].Data) {
		t.Fatal("episode bytes differ")
	}
	for _, s := range res.Sources {
		if !reRarName.MatchString(s.Name) {
			t.Fatalf("source %q kept its obfuscated name", s.Name)
		}
	}
}

func TestYencNamesRescueObfuscatedSubjects(t *testing.T) {
	e := newEnv(t)
	members, vols := rarSet(t)
	for i, v := range vols {
		e.post(fmt.Sprintf("PHMV2SbI78aIQ7wq%02d", i), fmt.Sprintf("show.part%02d.rar", i+1), v)
	}
	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Entries) != 2 || !bytes.Equal(e.read(res, res.Entries[0]), members[0].Data) {
		t.Fatalf("entries %v failures %v", entryNames(res), res.Failures)
	}
	if res.Sources[0].Name == "" || !reRarName.MatchString(res.Sources[0].Name) {
		t.Fatalf("source names %+v", res.Sources)
	}
}

func TestSampleLeftAfterMainArchiveFailedIsRejected(t *testing.T) {
	// Silicon Valley S01E02 ROVERS: the RAR set lost its first volume and
	// only a loose sample parsed. Importing that would replace the episode
	// with an 85 MB sample.
	e := newEnv(t)
	_, vols := rarSet(t)
	for i, v := range vols[1:] {
		e.post(fmt.Sprintf("show.part%02d.rar", i+2), fmt.Sprintf("show.part%02d.rar", i+2), v)
	}
	e.post("show.sample.mkv", "show.sample.mkv", random(5, 20_000))
	res, err := e.build()
	if !errors.Is(err, ErrLeftoverOnly) {
		t.Fatalf("got %v (entries %v failures %v)", err, entryNames(res), res.Failures)
	}
}

func TestSplitSevenZip(t *testing.T) {
	sevenZ, err := exec.LookPath("7z")
	if err != nil {
		t.Skip("7z not installed")
	}
	dir := t.TempDir()
	movie := random(6, 250_000)
	if err := os.WriteFile(filepath.Join(dir, "Film.mkv"), movie, 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(sevenZ, "a", "-mx=0", "-v70k", "film.7z", "Film.mkv")
	cmd.Dir = dir
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, b)
	}
	parts, _ := filepath.Glob(filepath.Join(dir, "film.7z.*"))
	e := newEnv(t)
	for _, p := range parts {
		b, _ := os.ReadFile(p)
		e.post(filepath.Base(p), filepath.Base(p), b)
	}
	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Entries) != 1 || res.Entries[0].Name != "Film.mkv" || !bytes.Equal(e.read(res, res.Entries[0]), movie) {
		t.Fatalf("entries %v failures %v", entryNames(res), res.Failures)
	}
}

// Same cases as decypharr's leftover_test.go.
func TestLeftoverRatio(t *testing.T) {
	const mb = int64(1) << 20
	cases := []struct {
		name         string
		kept, failed int64
		reject       bool
	}{
		{"sample left after main RAR failed", 85 * mb, 2400 * mb, true},
		{"nothing failed", 85 * mb, 0, false},
		{"only small extras failed", 2300 * mb, 3 * mb, false},
		{"real content beside a failed archive", 2300 * mb, 2400 * mb, false},
		{"season pack with most episodes failed", 1010 * mb, 1020 * mb, false},
		{"just at the ratio", 600 * mb, 2400 * mb, false},
		{"just under the ratio", 599 * mb, 2400 * mb, true},
	}
	for _, c := range cases {
		if got := LeftoverOnly(c.kept, c.failed); got != c.reject {
			t.Errorf("%s: reject=%v, want %v", c.name, got, c.reject)
		}
	}
}

// A media file rebuilt from an archive that doesn't start with its
// container's header (volumes stitched in the wrong order) is rejected as
// proof; the other members still import.
func TestBadMediaHeaderInArchiveIsRejected(t *testing.T) {
	e := newEnv(t)
	garbled := random(9, 400_000)
	copy(garbled, "not an mkv start")
	members := []rartest.Member{
		{Name: "Movie.2024.mkv", Data: garbled},
		{Name: "Movie.2024.mp4", Data: append([]byte{0, 0, 0, 0x20, 'f', 't', 'y', 'p'}, random(10, 100_000)...)},
	}
	for i, v := range rartest.Write(rartest.RAR5, members, 90_000) {
		name := fmt.Sprintf("movie.part%02d.rar", i+1)
		e.post(name, name, v)
	}
	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	if got := entryNames(res); len(got) != 1 || got[0] != "Movie.2024.mp4" {
		t.Fatalf("entries %v", got)
	}
	if len(res.Failures) != 1 || !strings.Contains(res.Failures[0].Error, "container header") {
		t.Fatalf("failures %+v", res.Failures)
	}
	if !mediaHeaderValid("x.mpg", []byte("anything")) || mediaHeaderValid("x.avi", []byte("RIFX....")) {
		t.Fatal("mpg is never judged; avi needs RIFF")
	}
}
