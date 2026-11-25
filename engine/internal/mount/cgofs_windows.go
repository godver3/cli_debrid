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
	fsys := &cgoFS{e: e, opt: opt, log: log, files: map[uint64]*openFile{}, sidecars: map[uint64]*sidecarWriter{}, ready: make(chan struct{})}
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

	mu       sync.Mutex
	next     uint64
	files    map[uint64]*openFile
	sidecars map[uint64]*sidecarWriter // handles of sidecars opened for writing
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
	ts := fuse.NewTimespec(stamp(mod))
	st.Mtim, st.Ctim, st.Atim, st.Birthtim = ts, ts, ts, ts
}

func (f *cgoFS) Getattr(path string, st *fuse.Stat_t, fh uint64) int {
	r := resolve(f.e, path)
	switch r.kind {
	case kindRoot, kindTop:
		f.fill(st, true, 0, time.Time{})
	case kindFolder:
		f.fill(st, true, 0, r.folder.ModTime)
		st.Mode = fuse.S_IFDIR | (0o755 &^ f.opt.Umask) // sidecars can be saved here
	case kindFile:
		f.fill(st, false, r.entry.Size, r.folder.ModTime)
	case kindSidecar:
		size := r.sidecar.Size
		if w := f.writer(fh); w != nil {
			size = w.size()
		}
		f.fill(st, false, size, r.sidecar.ModTime)
		st.Mode = fuse.S_IFREG | (0o644 &^ f.opt.Umask)
	default:
		return -fuse.ENOENT
	}
	return 0
}

func (f *cgoFS) writer(fh uint64) *sidecarWriter {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sidecars[fh]
}

func (f *cgoFS) addWriter(w *sidecarWriter) uint64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.next++
	f.sidecars[f.next] = w
	return f.next
}

// Create makes a subtitle sidecar in a job folder; nothing else is writable.
func (f *cgoFS) Create(path string, flags int, mode uint32) (int, uint64) {
	r := resolve(f.e, path)
	if r.kind == kindSidecar {
		return f.Open(path, flags|fuse.O_TRUNC)
	}
	if r.kind != kindNone || r.folder == nil || !engine.IsSidecarName(r.name) {
		return -fuse.EROFS, ^uint64(0)
	}
	w := newSidecarWriter(f.e, r.folder.JobID, r.name, true)
	if err := w.commit(); err != nil {
		f.log.Warn("sidecar create failed", "job", r.folder.JobID, "file", r.name, "err", err)
		return -fuse.EIO, ^uint64(0)
	}
	return 0, f.addWriter(w)
}

func (f *cgoFS) Write(path string, buf []byte, off int64, fh uint64) int {
	w := f.writer(fh)
	if w == nil {
		return -fuse.EROFS
	}
	n, err := w.writeAt(buf, off)
	if err != nil {
		return -fuse.EFBIG
	}
	return n
}

func (f *cgoFS) Truncate(path string, size int64, fh uint64) int {
	if w := f.writer(fh); w != nil {
		w.truncate(size)
		return 0
	}
	r := resolve(f.e, path)
	if r.kind != kindSidecar {
		return -fuse.EROFS
	}
	w := newSidecarWriter(f.e, r.folder.JobID, r.sidecar.Name, false)
	w.truncate(size)
	if w.commit() != nil {
		return -fuse.EIO
	}
	return 0
}

func (f *cgoFS) Flush(path string, fh uint64) int {
	if w := f.writer(fh); w != nil && w.commit() != nil {
		return -fuse.EIO
	}
	return 0
}

func (f *cgoFS) Unlink(path string) int {
	r := resolve(f.e, path)
	if r.kind != kindSidecar {
		return -fuse.EROFS
	}
	if f.e.DeleteSidecar(r.folder.JobID, r.sidecar.Name) != nil {
		return -fuse.EIO
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
		for _, sc := range f.e.Sidecars(r.folder.JobID) {
			st := &fuse.Stat_t{}
			f.fill(st, false, sc.Size, sc.ModTime)
			if !fill(sc.Name, st, 0) {
				break
			}
		}
	default:
		return -fuse.ENOENT
	}
	return 0
}

func (f *cgoFS) Open(path string, flags int) (int, uint64) {
	r := resolve(f.e, path)
	if r.kind == kindSidecar {
		if flags&(fuse.O_WRONLY|fuse.O_RDWR) != 0 {
			return 0, f.addWriter(newSidecarWriter(f.e, r.folder.JobID, r.sidecar.Name, flags&fuse.O_TRUNC != 0))
		}
		return 0, ^uint64(0) - 1 // reads go straight to disk
	}
	if flags&(fuse.O_WRONLY|fuse.O_RDWR) != 0 {
		return -fuse.EROFS, ^uint64(0)
	}
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
	if w := f.writer(fh); w != nil {
		return w.readAt(buf, off)
	}
	f.mu.Lock()
	of := f.files[fh]
	f.mu.Unlock()
	if of == nil {
		if r := resolve(f.e, path); r.kind == kindSidecar {
			n, err := readSidecar(f.e, r.folder.JobID, r.sidecar.Name, buf, off)
			if err != nil {
				return -fuse.EIO
			}
			return n
		}
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
	w := f.sidecars[fh]
	delete(f.files, fh)
	delete(f.sidecars, fh)
	f.mu.Unlock()
	if w != nil && w.commit() != nil {
		return -fuse.EIO
	}
	if of != nil {
		of.close()
	}
	return 0
}
