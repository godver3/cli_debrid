//go:build linux

// Package mount serves the engine's folder tree as a FUSE filesystem,
// read-only except for subtitle sidecars written into job folders:
//
//	<mount>/__all__/<job folder>/<file>
//	<mount>/{nzbs,torrents,__bad__,<provider>}/<job folder>/<file>
//
// The tree is looked up live from the engine, so new and deleted jobs
// show up without remounting.
package mount

import (
	"context"
	"hash/fnv"
	"log/slog"
	"sync"
	"syscall"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/job"
)

// Options for Mount.

// dirTTL keeps listings fresh enough that a finished import appears
// within a second; file attributes never change, so they are cached long.
const (
	dirTTL  = time.Second
	fileTTL = time.Hour
)

// Mount mounts e's tree at dir. Call Unmount on the returned server.
func Mount(e *engine.Engine, dir string, opt Options, log *slog.Logger) (*fuse.Server, error) {
	if log == nil {
		log = slog.Default()
	}
	r := &root{node: node{e: e, opt: opt, log: log}}
	dt, ft := dirTTL, fileTTL
	return fs.Mount(dir, r, &fs.Options{
		EntryTimeout:    &dt,
		AttrTimeout:     &ft,
		NegativeTimeout: &dt,
		UID:             opt.UID,
		GID:             opt.GID,
		MountOptions: fuse.MountOptions{
			AllowOther: opt.AllowOther,
			FsName:     "clid-engine",
			Name:       "clid",
			// Not "ro": subtitle sidecars can be written into job folders.
			// Every other write is refused per node with EROFS.
			Debug: opt.Debug,
			// Larger reads mean fewer round trips for players.
			MaxReadAhead: 1 << 20,
		},
	})
}

// node carries what every inode needs.
type node struct {
	fs.Inode
	e   *engine.Engine
	opt Options
	log *slog.Logger
}

func ino(parts ...string) uint64 {
	h := fnv.New64a()
	for _, p := range parts {
		h.Write([]byte(p))
		h.Write([]byte{0})
	}
	return h.Sum64() | 1 // never 0 or the root's 1
}

func (n *node) dirAttr(out *fuse.Attr, t time.Time) {
	out.Mode = syscall.S_IFDIR | 0o555&^n.opt.Umask
	out.Owner = fuse.Owner{Uid: n.opt.UID, Gid: n.opt.GID}
	out.SetTimes(&t, &t, &t)
}

// ---- root: the top folders ----------------------------------------------

type root struct{ node }

var _ = (fs.NodeReaddirer)((*root)(nil))
var _ = (fs.NodeLookuper)((*root)(nil))

func (r *root) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	var list []fuse.DirEntry
	for _, t := range r.e.TopFolders() {
		list = append(list, fuse.DirEntry{Name: t, Mode: syscall.S_IFDIR, Ino: ino(t)})
	}
	return fs.NewListDirStream(list), 0
}

func (r *root) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	for _, t := range r.e.TopFolders() {
		if t == name {
			r.dirAttr(&out.Attr, time.Time{})
			child := &top{node: node{e: r.e, opt: r.opt, log: r.log}, name: name}
			return r.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: ino(name)}), 0
		}
	}
	return nil, syscall.ENOENT
}

// ---- top folder: job folders ---------------------------------------------

type top struct {
	node
	name string
}

func (t *top) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	var list []fuse.DirEntry
	for _, f := range t.e.Folders(t.name) {
		list = append(list, fuse.DirEntry{Name: f.Name, Mode: syscall.S_IFDIR, Ino: ino(t.name, f.Name, f.JobID)})
	}
	return fs.NewListDirStream(list), 0
}

func (t *top) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	f, ok := t.e.Folder(t.name, name)
	if !ok {
		return nil, syscall.ENOENT
	}
	t.folderAttr(&out.Attr, f.ModTime)
	child := &folder{node: node{e: t.e, opt: t.opt, log: t.log}, top: t.name, name: name, jobID: f.JobID}
	return t.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: ino(t.name, name, f.JobID)}), 0
}

// ---- job folder: files -----------------------------------------------------

type folder struct {
	node
	top, name string
	jobID     string
}

// current returns the folder as the engine sees it now (it may have been
// renamed or deleted since the lookup).
func (d *folder) current() (*engine.Folder, bool) {
	f, ok := d.e.Folder(d.top, d.name)
	if !ok || f.JobID != d.jobID {
		return nil, false
	}
	return f, true
}

var _ = (fs.NodeCreater)((*folder)(nil))
var _ = (fs.NodeUnlinker)((*folder)(nil))
var _ = (fs.NodeGetattrer)((*folder)(nil))

// Job folders are writable (0755) so subtitle tools see they can save a
// sidecar there; the mount root and top folders stay read-only.
func (d *folder) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	f, ok := d.current()
	if !ok {
		return syscall.ENOENT
	}
	d.folderAttr(&out.Attr, f.ModTime)
	return 0
}

func (n *node) folderAttr(out *fuse.Attr, t time.Time) {
	n.dirAttr(out, t)
	out.Mode = syscall.S_IFDIR | 0o755&^n.opt.Umask
}

func (d *folder) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	f, ok := d.current()
	if !ok {
		return nil, syscall.ENOENT
	}
	var list []fuse.DirEntry
	for _, en := range f.Entries {
		list = append(list, fuse.DirEntry{Name: en.Name, Mode: syscall.S_IFREG, Ino: ino(d.jobID, en.Name)})
	}
	for _, sc := range d.e.Sidecars(d.jobID) {
		list = append(list, fuse.DirEntry{Name: sc.Name, Mode: syscall.S_IFREG, Ino: ino(d.jobID, "sidecar", sc.Name)})
	}
	return fs.NewListDirStream(list), 0
}

func (d *folder) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	f, ok := d.current()
	if !ok {
		return nil, syscall.ENOENT
	}
	for _, en := range f.Entries {
		if en.Name == name {
			fl := &file{node: node{e: d.e, opt: d.opt, log: d.log}, jobID: d.jobID, entry: en, mod: f.ModTime}
			fl.attr(&out.Attr)
			return d.NewInode(ctx, fl, fs.StableAttr{Mode: syscall.S_IFREG, Ino: ino(d.jobID, name)}), 0
		}
	}
	if _, ok := d.e.Sidecar(d.jobID, name, false); ok {
		sn := d.sidecarNode(name)
		sn.attr(&out.Attr)
		return d.NewInode(ctx, sn, fs.StableAttr{Mode: syscall.S_IFREG, Ino: ino(d.jobID, "sidecar", name)}), 0
	}
	return nil, syscall.ENOENT
}

func (d *folder) sidecarNode(name string) *sidecarFile {
	return &sidecarFile{node: node{e: d.e, opt: d.opt, log: d.log}, jobID: d.jobID, name: name}
}

// Create makes a subtitle sidecar; any other new file is refused.
func (d *folder) Create(ctx context.Context, name string, flags uint32, mode uint32, out *fuse.EntryOut) (*fs.Inode, fs.FileHandle, uint32, syscall.Errno) {
	if _, ok := d.current(); !ok {
		return nil, nil, 0, syscall.ENOENT
	}
	if !engine.IsSidecarName(name) {
		return nil, nil, 0, syscall.EROFS
	}
	for _, en := range d.mustEntries() {
		if en.Name == name {
			return nil, nil, 0, syscall.EEXIST
		}
	}
	sn := d.sidecarNode(name)
	w := newSidecarWriter(d.e, d.jobID, name, true)
	// Store the empty file now so the name exists for the lookups that follow.
	if err := w.commit(); err != nil {
		d.log.Warn("sidecar create failed", "job", d.jobID, "file", name, "err", err)
		return nil, nil, 0, syscall.EIO
	}
	sn.attrSize(&out.Attr, 0)
	in := d.NewInode(ctx, sn, fs.StableAttr{Mode: syscall.S_IFREG, Ino: ino(d.jobID, "sidecar", name)})
	return in, sn.track(w), fuse.FOPEN_DIRECT_IO, 0
}

func (d *folder) mustEntries() []job.Entry {
	if f, ok := d.current(); ok {
		return f.Entries
	}
	return nil
}

// Unlink deletes a sidecar; streamed files can't be deleted here.
func (d *folder) Unlink(ctx context.Context, name string) syscall.Errno {
	if _, ok := d.e.Sidecar(d.jobID, name, false); !ok {
		return syscall.EROFS
	}
	if err := d.e.DeleteSidecar(d.jobID, name); err != nil {
		return syscall.EIO
	}
	return 0
}

// ---- sidecar file ----------------------------------------------------------

type sidecarFile struct {
	node
	jobID, name string

	mu      sync.Mutex
	writers map[*sidecarWriter]struct{} // handles open for writing
}

// track registers a write handle; a path truncate (the kernel's O_TRUNC
// without a file handle) must reach buffers that are already open.
func (s *sidecarFile) track(w *sidecarWriter) *sidecarHandle {
	s.mu.Lock()
	if s.writers == nil {
		s.writers = map[*sidecarWriter]struct{}{}
	}
	s.writers[w] = struct{}{}
	s.mu.Unlock()
	return &sidecarHandle{w: w, node: s}
}

func (s *sidecarFile) untrack(w *sidecarWriter) {
	s.mu.Lock()
	delete(s.writers, w)
	s.mu.Unlock()
}

func (s *sidecarFile) openWriters() []*sidecarWriter {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*sidecarWriter, 0, len(s.writers))
	for w := range s.writers {
		out = append(out, w)
	}
	return out
}

var _ = (fs.NodeOpener)((*sidecarFile)(nil))
var _ = (fs.NodeReader)((*sidecarFile)(nil))
var _ = (fs.NodeWriter)((*sidecarFile)(nil))
var _ = (fs.NodeGetattrer)((*sidecarFile)(nil))
var _ = (fs.NodeSetattrer)((*sidecarFile)(nil))
var _ = (fs.NodeFlusher)((*sidecarFile)(nil))

func (s *sidecarFile) attrSize(out *fuse.Attr, size int64) {
	sc, _ := s.e.Sidecar(s.jobID, s.name, false)
	out.Mode = syscall.S_IFREG | 0o644&^s.opt.Umask
	out.Size = uint64(size)
	out.Blocks = (out.Size + 511) / 512
	out.Owner = fuse.Owner{Uid: s.opt.UID, Gid: s.opt.GID}
	out.SetTimes(&sc.ModTime, &sc.ModTime, &sc.ModTime)
}

func (s *sidecarFile) attr(out *fuse.Attr) {
	sc, _ := s.e.Sidecar(s.jobID, s.name, false)
	s.attrSize(out, sc.Size)
}

func (s *sidecarFile) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	if h, ok := fh.(*sidecarHandle); ok && h.w != nil {
		s.attrSize(&out.Attr, h.w.size())
		return 0
	}
	if _, ok := s.e.Sidecar(s.jobID, s.name, false); !ok {
		return syscall.ENOENT
	}
	s.attr(&out.Attr)
	return 0
}

func (s *sidecarFile) Setattr(ctx context.Context, fh fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	if size, ok := in.GetSize(); ok {
		h, isW := fh.(*sidecarHandle)
		if !isW || h.w == nil {
			// truncate on a path (also how the kernel applies O_TRUNC
			// right after an open): open buffers and the stored file.
			for _, ow := range s.openWriters() {
				ow.truncate(int64(size))
			}
			w := newSidecarWriter(s.e, s.jobID, s.name, false)
			w.truncate(int64(size))
			if err := w.commit(); err != nil {
				return syscall.EIO
			}
			s.attr(&out.Attr)
			return 0
		}
		h.w.truncate(int64(size))
		s.attrSize(&out.Attr, h.w.size())
		return 0
	}
	return s.Getattr(ctx, fh, out) // times/modes: accepted, not stored
}

func (s *sidecarFile) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_WRONLY|syscall.O_RDWR) != 0 {
		return s.track(newSidecarWriter(s.e, s.jobID, s.name, flags&syscall.O_TRUNC != 0)), fuse.FOPEN_DIRECT_IO, 0
	}
	return &sidecarHandle{}, fuse.FOPEN_DIRECT_IO, 0
}

func (s *sidecarFile) Read(ctx context.Context, fh fs.FileHandle, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	if h, ok := fh.(*sidecarHandle); ok && h.w != nil {
		return fuse.ReadResultData(dest[:h.w.readAt(dest, off)]), 0
	}
	n, err := readSidecar(s.e, s.jobID, s.name, dest, off)
	if err != nil {
		return nil, syscall.EIO
	}
	return fuse.ReadResultData(dest[:n]), 0
}

func (s *sidecarFile) Write(ctx context.Context, fh fs.FileHandle, data []byte, off int64) (uint32, syscall.Errno) {
	h, ok := fh.(*sidecarHandle)
	if !ok || h.w == nil {
		return 0, syscall.EBADF
	}
	n, err := h.w.writeAt(data, off)
	if err != nil {
		return 0, syscall.EFBIG
	}
	return uint32(n), 0
}

func (s *sidecarFile) Flush(ctx context.Context, fh fs.FileHandle) syscall.Errno {
	if h, ok := fh.(*sidecarHandle); ok && h.w != nil {
		if err := h.w.commit(); err != nil {
			s.log.Warn("sidecar save failed", "job", s.jobID, "file", s.name, "err", err)
			return syscall.EIO
		}
	}
	return 0
}

// sidecarHandle: w is set for handles opened for writing.
type sidecarHandle struct {
	w    *sidecarWriter
	node *sidecarFile
}

var _ = (fs.FileReleaser)((*sidecarHandle)(nil))

func (h *sidecarHandle) Release(ctx context.Context) syscall.Errno {
	if h.w != nil {
		if h.node != nil {
			h.node.untrack(h.w)
		}
		if err := h.w.commit(); err != nil {
			return syscall.EIO
		}
	}
	return 0
}

// ---- file ------------------------------------------------------------------

type file struct {
	node
	jobID string
	entry job.Entry
	mod   time.Time
}

var _ = (fs.NodeOpener)((*file)(nil))
var _ = (fs.NodeReader)((*file)(nil))
var _ = (fs.NodeGetattrer)((*file)(nil))

func (f *file) attr(out *fuse.Attr) {
	out.Mode = syscall.S_IFREG | 0o444&^f.opt.Umask
	out.Size = uint64(f.entry.Size)
	out.Blocks = (out.Size + 511) / 512
	out.Blksize = 1 << 20
	out.Owner = fuse.Owner{Uid: f.opt.UID, Gid: f.opt.GID}
	out.SetTimes(&f.mod, &f.mod, &f.mod)
}

func (f *file) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	f.attr(&out.Attr)
	return 0
}

func (f *file) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_WRONLY|syscall.O_RDWR) != 0 {
		return nil, 0, syscall.EROFS
	}
	of, err := openEntry(f.e, f.log, f.jobID, f.entry)
	if err != nil {
		return nil, 0, syscall.EIO
	}
	// Keep the kernel page cache between opens: content never changes.
	return &handle{of: of}, fuse.FOPEN_KEEP_CACHE, 0
}

func (f *file) Read(ctx context.Context, fh fs.FileHandle, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	h, ok := fh.(*handle)
	if !ok {
		return nil, syscall.EBADF
	}
	return h.read(ctx, dest, off)
}

var _ = (fs.FileReleaser)((*handle)(nil))

// handle adapts an openFile to go-fuse.
type handle struct{ of *openFile }

// Release ends the stream record when the file is closed.
func (h *handle) Release(ctx context.Context) syscall.Errno {
	h.of.close()
	return 0
}

func (h *handle) read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	n, err := h.of.readAt(ctx, dest, off)
	if err != nil {
		return nil, syscall.EIO
	}
	return fuse.ReadResultData(dest[:n]), 0
}
