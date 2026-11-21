package job

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/archive/rartest"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

const jobPassword = "p&ss<wörd>"

func lockedMembers() []rartest.Member {
	return []rartest.Member{
		{Name: "Locked.S01E01.1080p.mkv", Data: random(21, 400_005)},
		{Name: "Sample/locked.sample.mkv", Data: random(22, 30_001)},
	}
}

func (e *env) postEncryptedRAR(opt rartest.Options) []rartest.Member {
	members := lockedMembers()
	for i, v := range rartest.WriteOptions(rartest.RAR5, members, 90_000, opt) {
		name := fmt.Sprintf("locked.part%02d.rar", i+1)
		e.post(name, name, v)
	}
	return members
}

// checkEntries reads every member back through job.Open, before and after
// a JSON round trip (stored jobs reopen from result.json).
func (e *env) checkEntries(res *Result, members []rartest.Member) {
	e.t.Helper()
	b, err := json.Marshal(res)
	if err != nil {
		e.t.Fatal(err)
	}
	var back Result
	if err := json.Unmarshal(b, &back); err != nil {
		e.t.Fatal(err)
	}
	for _, r := range []*Result{res, &back} {
		byName := map[string]Entry{}
		for _, en := range r.Entries {
			byName[en.Name] = en
		}
		for _, m := range members {
			en, ok := byName[filepath.Base(m.Name)]
			if !ok || en.Encryption == nil || en.Size != int64(len(m.Data)) {
				e.t.Fatalf("%s: entry %+v (entries %v, failures %v)", m.Name, en, entryNames(r), r.Failures)
			}
			if !bytes.Equal(e.read(r, en), m.Data) {
				e.t.Fatalf("%s: decrypted data mismatch", m.Name)
			}
		}
	}
}

func TestEncryptedRAR5PasswordFromNZBMeta(t *testing.T) {
	e := newEnv(t)
	members := e.postEncryptedRAR(rartest.Options{Password: jobPassword})
	// nzbparser hands meta values over still XML-escaped.
	e.nzb.Meta = map[string]string{"password": "p&amp;ss&lt;wörd&gt;"}
	res, err := e.build()
	if err != nil {
		t.Fatal(err)
	}
	e.checkEntries(res, members)
	for _, n := range res.Notes {
		if strings.Contains(n, "wörd") {
			t.Fatalf("password leaked into notes: %q", n)
		}
	}
}

func TestEncryptedRAR5HeadersWithPasswordOption(t *testing.T) {
	e := newEnv(t)
	members := e.postEncryptedRAR(rartest.Options{Password: jobPassword, EncryptHeaders: true})
	res, err := Build(context.Background(), e.c, e.cache, e.nzb, Options{Parallel: 4, Password: jobPassword})
	if err != nil {
		t.Fatal(err)
	}
	e.checkEntries(res, members)
}

func TestEncryptedRAR5WithoutOrWrongPasswordFailsFinal(t *testing.T) {
	for _, tc := range []struct {
		name, password, want string
		headers              bool
	}{
		{"no password", "", "password needed", false},
		{"no password, encrypted headers", "", "password needed", true},
		{"wrong password", "nope", "wrong password", false},
		{"wrong password, encrypted headers", "nope", "wrong password", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			e.postEncryptedRAR(rartest.Options{Password: jobPassword, EncryptHeaders: tc.headers})
			res, err := Build(context.Background(), e.c, e.cache, e.nzb, Options{Parallel: 4, Password: tc.password})
			if !errors.Is(err, ErrNothingStreamable) || errors.Is(err, ErrInconclusive) {
				t.Fatalf("want a final nothing-streamable error, got %v", err)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error should say %q: %v (failures %+v)", tc.want, err, res.Failures)
			}
		})
	}
}

func TestEncryptedSevenZipStreams(t *testing.T) {
	sevenZ, err := exec.LookPath("7z")
	if err != nil {
		t.Skip("7z not installed")
	}
	for _, headers := range []bool{false, true} {
		t.Run(fmt.Sprintf("headers=%v", headers), func(t *testing.T) {
			dir := t.TempDir()
			movie := random(23, 250_007)
			if err := os.WriteFile(filepath.Join(dir, "Locked.Film.mkv"), movie, 0o644); err != nil {
				t.Fatal(err)
			}
			he := "-mhe=off"
			if headers {
				he = "-mhe=on"
			}
			cmd := exec.Command(sevenZ, "a", "-mx=0", he, "-p"+jobPassword, "-v70k", "film.7z", "Locked.Film.mkv")
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
			e.nzb.Meta = map[string]string{"password": "<![CDATA[" + jobPassword + "]]>"}
			res, err := e.build()
			if err != nil {
				t.Fatal(err)
			}
			e.checkEntries(res, []rartest.Member{{Name: "Locked.Film.mkv", Data: movie}})
		})
	}
}

func TestNZBPassword(t *testing.T) {
	for meta, want := range map[string]string{
		"plain":               "plain",
		"  spaced  ":          "spaced",
		"a&amp;b":             "a&b",
		"<![CDATA[x&amp;y]]>": "x&amp;y",
		"":                    "",
	} {
		n := &nzb.NZB{Meta: map[string]string{"Password": meta}}
		if got := NZBPassword(n); got != want {
			t.Fatalf("%q: got %q want %q", meta, got, want)
		}
	}
	if NZBPassword(nil) != "" || NZBPassword(&nzb.NZB{}) != "" {
		t.Fatal("no meta means no password")
	}
}

// Migrated and new-style encrypted readers must agree: decrypting with a
// skip lands on the same plaintext as decrypting the whole stream.
func TestEntryDecryptSkip(t *testing.T) {
	e := newEnv(t)
	members := e.postEncryptedRAR(rartest.Options{Password: jobPassword})
	res, err := Build(context.Background(), e.c, e.cache, e.nzb, Options{Parallel: 4, Password: jobPassword})
	if err != nil {
		t.Fatal(err)
	}
	en := res.Entries[0]
	if en.Name != filepath.Base(members[0].Name) {
		t.Fatalf("largest entry %s", en.Name)
	}
	// A member that starts 48 bytes into the stream: same pieces, skip 48,
	// size reduced by 48.
	shifted := en
	enc := *en.Encryption
	enc.Skip = 48
	shifted.Encryption = &enc
	shifted.Size = en.Size - 48
	got := e.read(res, shifted)
	if !bytes.Equal(got, members[0].Data[48:]) {
		t.Fatal("skip read mismatch")
	}
}
