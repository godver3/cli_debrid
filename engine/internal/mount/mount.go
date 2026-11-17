//go:build linux

// Package mount serves the engine's folder tree as a read-only FUSE
// filesystem:
//
//	<mount>/__all__/<job folder>/<file>
//	<mount>/nzbs/<job folder>/<file>
//
// The tree is looked up live from the engine, so new and deleted jobs
// show up without remounting.
package mount

import (
	"context"
	"hash/fnv"
	"log/slog"
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
			Options:    []string{"ro"},
			Debug:      opt.Debug,
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
	t.dirAttr(&out.Attr, f.ModTime)
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

func (d *folder) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	f, ok := d.current()
	if !ok {
		return nil, syscall.ENOENT
	}
	var list []fuse.DirEntry
	for _, en := range f.Entries {
		list = append(list, fuse.DirEntry{Name: en.Name, Mode: syscall.S_IFREG, Ino: ino(d.jobID, en.Name)})
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
	return nil, syscall.ENOENT
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
