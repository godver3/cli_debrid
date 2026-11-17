//go:build windows

package mount

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/winfsp/cgofuse/fuse"

	"github.com/godver3/cli_debrid/engine/internal/engine"
)

// Windows: the same tree served through WinFsp (cgofuse, no cgo needed on
// Windows). dir is a drive letter ("M:") or an empty folder that doesn't
// exist yet. Requires WinFsp (https://winfsp.dev) to be installed.

// Start mounts the engine at dir with WinFsp.
func Start(e *engine.Engine, dir string, opt Options, log *slog.Logger) (Mounted, error) {
	fsys := &cgoFS{e: e, opt: opt, log: log, files: map[uint64]*openFile{}, ready: make(chan struct{})}
	host := fuse.NewFileSystemHost(fsys)
	host.SetCapReaddirPlus(true)
	host.SetCapCaseInsensitive(true) // Windows paths are case-insensitive
	opts := []string{"-o", "uid=-1,gid=-1", "-o", "volname=clid", "--FileSystemName=clid", "-o", "fsname=clid-engine"}
	failed := make(chan struct{})
	go func() {
		if !host.Mount(dir, opts) {
			close(failed)
		}
	}()
	select {
	case <-fsys.ready:
		return winMount{host: host}, nil
	case <-failed:
		return nil, errors.New("WinFsp could not mount " + dir + " (is WinFsp installed, and is the drive letter free / the folder absent?)")
	case <-time.After(30 * time.Second):
		host.Unmount()
		return nil, fmt.Errorf("WinFsp mount of %s timed out", dir)
	}
}

type winMount struct{ host *fuse.FileSystemHost }

func (m winMount) Unmount() error {
	if !m.host.Unmount() {
		return errors.New("WinFsp unmount failed")
	}
	return nil
}

type cgoFS struct {
	fuse.FileSystemBase
	e     *engine.Engine
	opt   Options
	log   *slog.Logger
	ready chan struct{}
	once  sync.Once

	mu    sync.Mutex
	next  uint64
	files map[uint64]*openFile
}

func (f *cgoFS) Init() { f.once.Do(func() { close(f.ready) }) }

func (f *cgoFS) Statfs(path string, st *fuse.Statfs_t) int {
	st.Bsize, st.Frsize, st.Namemax = 1<<20, 1<<20, 255
	return 0
}

func (f *cgoFS) fill(st *fuse.Stat_t, dir bool, size int64, mod time.Time) {
	if dir {
		st.Mode = fuse.S_IFDIR | (0o555 &^ f.opt.Umask)
		st.Nlink = 2
	} else {
		st.Mode = fuse.S_IFREG | (0o444 &^ f.opt.Umask)
		st.Nlink = 1
		st.Size = size
		st.Blksize = 1 << 20
		st.Blocks = (size + 511) / 512
	}
	ts := fuse.NewTimespec(mod)
	st.Mtim, st.Ctim, st.Atim, st.Birthtim = ts, ts, ts, ts
}

func (f *cgoFS) Getattr(path string, st *fuse.Stat_t, fh uint64) int {
	r := resolve(f.e, path)
	switch r.kind {
	case kindRoot, kindTop:
		f.fill(st, true, 0, time.Time{})
	case kindFolder:
		f.fill(st, true, 0, r.folder.ModTime)
	case kindFile:
		f.fill(st, false, r.entry.Size, r.folder.ModTime)
	default:
		return -fuse.ENOENT
	}
	return 0
}

func (f *cgoFS) Opendir(path string) (int, uint64) {
	switch resolve(f.e, path).kind {
	case kindRoot, kindTop, kindFolder:
		return 0, 0
	}
	return -fuse.ENOENT, ^uint64(0)
}

func (f *cgoFS) Readdir(path string, fill func(string, *fuse.Stat_t, int64) bool, ofst int64, fh uint64) int {
	r := resolve(f.e, path)
	dot := &fuse.Stat_t{}
	f.fill(dot, true, 0, time.Time{})
	fill(".", dot, 0)
	fill("..", dot, 0)
	switch r.kind {
	case kindRoot:
		for _, t := range f.e.TopFolders() {
			st := &fuse.Stat_t{}
			f.fill(st, true, 0, time.Time{})
			if !fill(t, st, 0) {
				break
			}
		}
	case kindTop:
		for _, d := range f.e.Folders(r.top) {
			st := &fuse.Stat_t{}
			f.fill(st, true, 0, d.ModTime)
			if !fill(d.Name, st, 0) {
				break
			}
		}
	case kindFolder:
		for _, en := range r.folder.Entries {
			st := &fuse.Stat_t{}
			f.fill(st, false, en.Size, r.folder.ModTime)
			if !fill(en.Name, st, 0) {
				break
			}
		}
	default:
		return -fuse.ENOENT
	}
	return 0
}

func (f *cgoFS) Open(path string, flags int) (int, uint64) {
	if flags&(fuse.O_WRONLY|fuse.O_RDWR) != 0 {
		return -fuse.EROFS, ^uint64(0)
	}
	r := resolve(f.e, path)
	if r.kind != kindFile {
		return -fuse.ENOENT, ^uint64(0)
	}
	of, err := openEntry(f.e, f.log, r.folder.JobID, r.entry)
	if err != nil {
		return -fuse.EIO, ^uint64(0)
	}
	f.mu.Lock()
	f.next++
	fh := f.next
	f.files[fh] = of
	f.mu.Unlock()
	return 0, fh
}

func (f *cgoFS) Read(path string, buf []byte, off int64, fh uint64) int {
	f.mu.Lock()
	of := f.files[fh]
	f.mu.Unlock()
	if of == nil {
		return -fuse.EBADF
	}
	n, err := of.readAt(context.Background(), buf, off)
	if err != nil {
		return -fuse.EIO
	}
	return n
}

func (f *cgoFS) Release(path string, fh uint64) int {
	f.mu.Lock()
	of := f.files[fh]
	delete(f.files, fh)
	f.mu.Unlock()
	if of != nil {
		of.close()
	}
	return 0
}
