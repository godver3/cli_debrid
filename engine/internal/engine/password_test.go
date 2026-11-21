package engine

import (
	"bytes"
	"fmt"
	"strings"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

func encryptedNZB(s *nntptest.Server, password string, headers bool) ([]byte, []byte) {
	movie := data(31, 400_009)
	copy(movie, []byte{0x1A, 0x45, 0xDF, 0xA3}) // archive members are header-checked
	vols := rartest.WriteOptions(rartest.RAR5, []rartest.Member{{Name: "Locked.Movie.2024.mkv", Data: movie}}, 90_000,
		rartest.Options{Password: password, EncryptHeaders: headers})
	var files []nntptest.File
	for i, v := range vols {
		files = append(files, nntptest.File{Name: fmt.Sprintf("locked.part%02d.rar", i+1), Data: v})
	}
	return nntptest.PostNZB(s, "lk", files...), movie
}

func TestPasswordFromJobNameImportsEncryptedSet(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	nz, movie := encryptedNZB(s, "hunter2&co", true)
	j, err := e.Submit(nz, "Locked.Movie.2024{{hunter2&co}}", "movies", "")
	if err != nil {
		t.Fatal(err)
	}
	if j.Name != "Locked.Movie.2024" {
		t.Fatalf("password must not stay in the job name: %q", j.Name)
	}
	got := waitDone(t, e, j.ID)
	if got.Status != store.Ready {
		t.Fatalf("import: %s %q", got.Status, got.Error)
	}
	stored, err := e.Store.NZB(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := nzb.Parse(bytes.NewReader(stored))
	if err != nil || job.NZBPassword(parsed) != "hunter2&co" {
		t.Fatalf("stored NZB should carry the password meta (err %v)", err)
	}
	_, full, err := e.Store.Full(j.ID)
	if err != nil {
		t.Fatal(err)
	}
	fs, err := job.Open(full, parsed, e.Client, e.Cache, stream.ReadAhead{})
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, len(movie))
	if n, err := fs.Entry(full.Entries[0]).ReadAt(buf, 0); n != len(buf) || !bytes.Equal(buf, movie) {
		t.Fatalf("read %d: %v", n, err)
	}
}

func TestSubmitPasswordParamAndWrongPassword(t *testing.T) {
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	nz, _ := encryptedNZB(s, "right", false)
	j, _ := e.SubmitPassword(nz, "Locked", "movies", "", "right")
	if got := waitDone(t, e, j.ID); got.Status != store.Ready {
		t.Fatalf("import: %s %q", got.Status, got.Error)
	}
	nz2, _ := encryptedNZB(s, "right", false)
	j2, _ := e.SubmitPassword(nz2, "Locked again", "movies", "", "wrong")
	got := waitDone(t, e, j2.ID)
	if got.Status != store.Failed || got.Retryable || !strings.Contains(got.Error, "wrong password") {
		t.Fatalf("wrong password must fail final: %s retryable=%v %q", got.Status, got.Retryable, got.Error)
	}
}

func TestWithPasswordMeta(t *testing.T) {
	base := `<?xml version="1.0"?><nzb xmlns="x"><file poster="p" date="1" subject="s"><groups><group>g</group></groups><segments><segment bytes="1" number="1">a@b</segment></segments></file></nzb>`
	withHead := strings.Replace(base, `<file`, `<head><meta type="title">T</meta></head><file`, 1)
	for name, in := range map[string]string{"no head": base, "head": withHead} {
		out, ok := withPasswordMeta([]byte(in), `a<b>&"c"`)
		if !ok {
			t.Fatalf("%s: not added", name)
		}
		parsed, err := nzb.Parse(bytes.NewReader(out))
		if err != nil || job.NZBPassword(parsed) != `a<b>&"c"` {
			t.Fatalf("%s: %v %q", name, err, out)
		}
		if name == "head" && parsed.Meta["title"] != "T" {
			t.Fatalf("existing meta lost: %q", out)
		}
	}
	if _, ok := withPasswordMeta([]byte("garbage"), "x"); ok {
		t.Fatal("garbage must be left alone")
	}
}

func TestSplitNamePassword(t *testing.T) {
	for in, want := range map[string][2]string{
		"Movie{{pw}}":       {"Movie", "pw"},
		"Movie {{p w}}.nzb": {"Movie", "p w"},
		"{{pw}}Movie":       {"Movie", "pw"},
		"Movie":             {"Movie", ""},
		"Movie {not} {{}}":  {"Movie {not}", ""},
	} {
		name, pw := splitNamePassword(in)
		if name != want[0] || pw != want[1] {
			t.Fatalf("%q: got %q/%q want %q/%q", in, name, pw, want[0], want[1])
		}
	}
}
