//go:build linux

package mount

import (
	"log/slog"

	"github.com/godver3/cli_debrid/engine/internal/engine"
)

// Start mounts the engine at dir with go-fuse (the fastest Linux backend),
// clearing a dead mount left on dir first.
func Start(e *engine.Engine, dir string, opt Options, log *slog.Logger) (Mounted, error) {
	ClearStale(dir, log)
	srv, err := Mount(e, dir, opt, log)
	if err != nil {
		return nil, err
	}
	return linuxMount{srv: srv, dir: dir, log: log}, nil
}

type linuxMount struct {
	srv interface{ Unmount() error }
	dir string
	log *slog.Logger
}

// Unmount detaches the mount; if it is busy (Plex has a file open) it falls
// back to a lazy unmount, or the next start would stack on a dead mount.
func (m linuxMount) Unmount() error {
	if err := m.srv.Unmount(); err != nil {
		if lerr := LazyUnmount(m.dir); lerr != nil {
			return lerr
		}
		m.log.Info("unmounted lazily (files were still open)")
	}
	return nil
}
