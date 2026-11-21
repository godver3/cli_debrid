package mount

import (
	"io"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

// A mount left behind (the engine was killed, or unmount hit "busy") is
// cleared before the next mount instead of being stacked on.
func TestClearStaleRemovesLeftoverMount(t *testing.T) {
	fuseAvailable(t)
	s := nntptest.Start(t)
	host, port := s.Addr()
	e, err := engine.New(engine.Config{DataDir: t.TempDir(), MountPath: "unused",
		Providers: []nntp.Provider{{Name: "t", Host: host, Port: port, MaxConns: 2, Timeout: 5 * time.Second}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	dir := t.TempDir()
	srv, err := Mount(e, dir, Options{UID: uint32(os.Getuid()), GID: uint32(os.Getgid())}, nil)
	if err != nil {
		t.Skipf("mount not permitted here: %v", err)
	}
	defer srv.Unmount()
	if n := ourMounts(dir); n != 1 {
		t.Fatalf("ourMounts = %d, want 1", n)
	}
	// Hold a file handle open so a plain unmount would be "busy".
	f, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	ClearStale(dir, slogDiscard())
	if n := ourMounts(dir); n != 0 {
		t.Fatalf("after ClearStale ourMounts = %d, want 0", n)
	}
	if ourMounts(t.TempDir()) != 0 {
		t.Fatal("unrelated path reported as mounted")
	}
}

func slogDiscard() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }
