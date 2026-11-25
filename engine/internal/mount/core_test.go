package mount

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"math/rand"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// The path-based core the Windows (WinFsp) mount is built on, tested
// without mounting: lookups (case-insensitive, either slash), reads, and
// stream records.
func TestPathCoreResolveAndRead(t *testing.T) {
	s := nntptest.Start(t)
	host, port := s.Addr()
	e, err := engine.New(engine.Config{DataDir: t.TempDir(), MountPath: "M:",
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 4, Timeout: 5 * time.Second}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	data := make([]byte, 300_000)
	rand.New(rand.NewSource(1)).Read(data)
	j, _ := e.Submit(nntptest.PostNZB(s, "m", nntptest.File{Name: "Movie.mkv", Data: data}), "Movie", "movies", "")
	for i := 0; i < 500; i++ {
		if g, _, _ := e.Store.Get(j.ID); g.Status == store.Ready {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	cases := map[string]int{`\`: kindRoot, "/__all__": kindTop, `\__ALL__\movie`: kindFolder,
		"/__all__/Movie/movie.MKV": kindFile, "/nope": kindNone, "/__all__/Movie/x.mkv": kindNone, "/__all__/Movie/Movie.mkv/x": kindNone}
	for p, want := range cases {
		if got := resolve(e, p).kind; got != want {
			t.Errorf("resolve(%q) = %d, want %d", p, got, want)
		}
	}
	r := resolve(e, `\__all__\Movie\Movie.mkv`)
	of, err := openEntry(e, slog.New(slog.NewTextHandler(io.Discard, nil)), r.folder.JobID, r.entry)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1000)
	if n, err := of.readAt(context.Background(), buf, 250_000); err != nil || n != 1000 || !bytes.Equal(buf, data[250_000:251_000]) {
		t.Fatalf("read: %d %v", n, err)
	}
	if n, err := of.readAt(context.Background(), buf, 299_500); err != nil || n != 500 {
		t.Fatalf("short read at EOF: %d %v", n, err)
	}
	if ss := e.Streams(); len(ss) != 1 || ss[0].BytesRead != 1500 {
		t.Fatalf("streams: %+v", ss)
	}
	of.close()
	if len(e.Streams()) != 0 {
		t.Fatal("stream not closed")
	}
}
