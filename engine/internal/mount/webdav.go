package mount

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"mime"
	"net/http"
	"os"
	"path"
	"strings"
	"time"

	"golang.org/x/net/webdav"

	"github.com/godver3/cli_debrid/engine/internal/engine"
)

// WebDAV serves the same tree as the FUSE mount, read-only, for machines
// that can't see the mount: Plex on another host (rclone mount of this
// share), or Windows without WinFsp. Reads go through the same path as
// the mount (playback priority, now playing, repair on proven misses).

// WebDAVOptions configures WebDAVHandler.
type WebDAVOptions struct {
	User     string
	Password string
}

// WebDAVHandler returns the read-only WebDAV share (basic auth when a
// user is set).
func WebDAVHandler(e *engine.Engine, opt WebDAVOptions, log *slog.Logger) http.Handler {
	if log == nil {
		log = slog.Default()
	}
	h := &webdav.Handler{
		FileSystem: davFS{e: e, log: log},
		LockSystem: webdav.NewMemLS(),
		Logger: func(r *http.Request, err error) {
			if err != nil && !errors.Is(err, os.ErrNotExist) && !errors.Is(err, context.Canceled) {
				log.Debug("webdav", "method", r.Method, "path", r.URL.Path, "err", err)
			}
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if opt.User != "" {
			u, p, ok := r.BasicAuth()
			if !ok || subtle.ConstantTimeCompare([]byte(u), []byte(opt.User)) != 1 ||
				subtle.ConstantTimeCompare([]byte(p), []byte(opt.Password)) != 1 {
				w.Header().Set("WWW-Authenticate", `Basic realm="clid"`)
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
		}
		switch r.Method {
		case http.MethodGet, http.MethodHead, http.MethodOptions, "PROPFIND":
			h.ServeHTTP(w, r)
		default:
			http.Error(w, "read-only share", http.StatusForbidden)
		}
	})
}

type davFS struct {
	e   *engine.Engine
	log *slog.Logger
}

func (davFS) Mkdir(context.Context, string, os.FileMode) error { return os.ErrPermission }
func (davFS) RemoveAll(context.Context, string) error          { return os.ErrPermission }
func (davFS) Rename(context.Context, string, string) error     { return os.ErrPermission }

func (d davFS) Stat(ctx context.Context, name string) (os.FileInfo, error) {
	r := resolve(d.e, name)
	if r.kind == kindNone {
		return nil, os.ErrNotExist
	}
	return d.info(r), nil
}

func (d davFS) info(r resolved) davInfo {
	switch r.kind {
	case kindRoot:
		return davInfo{name: "/", dir: true}
	case kindTop:
		return davInfo{name: r.top, dir: true}
	case kindFolder:
		return davInfo{name: r.folder.Name, dir: true, mod: r.folder.ModTime}
	case kindFile:
		return davInfo{name: r.entry.Name, size: r.entry.Size, mod: r.folder.ModTime, etag: r.folder.JobID}
	case kindSidecar:
		return davInfo{name: r.sidecar.Name, size: r.sidecar.Size, mod: r.sidecar.ModTime}
	}
	return davInfo{}
}

func (d davFS) OpenFile(ctx context.Context, name string, flag int, perm os.FileMode) (webdav.File, error) {
	if flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC|os.O_APPEND) != 0 {
		return nil, os.ErrPermission
	}
	r := resolve(d.e, name)
	switch r.kind {
	case kindNone:
		return nil, os.ErrNotExist
	case kindSidecar:
		p, err := d.e.SidecarPath(r.folder.JobID, r.sidecar.Name)
		if err != nil {
			return nil, os.ErrNotExist
		}
		return os.Open(p)
	case kindFile:
		return &davFile{fs: d, r: r, info: d.info(r), ctx: ctx}, nil
	}
	return &davDir{fs: d, r: r, info: d.info(r)}, nil
}

// ---- directories -------------------------------------------------------------

type davDir struct {
	fs   davFS
	r    resolved
	info davInfo
	done bool
}

func (*davDir) Close() error                   { return nil }
func (*davDir) Read([]byte) (int, error)       { return 0, fmt.Errorf("is a directory") }
func (*davDir) Seek(int64, int) (int64, error) { return 0, nil }
func (*davDir) Write([]byte) (int, error)      { return 0, os.ErrPermission }
func (d *davDir) Stat() (os.FileInfo, error)   { return d.info, nil }

func (d *davDir) Readdir(count int) ([]os.FileInfo, error) {
	if d.done {
		if count > 0 {
			return nil, io.EOF
		}
		return nil, nil
	}
	d.done = true
	e := d.fs.e
	var out []os.FileInfo
	switch d.r.kind {
	case kindRoot:
		for _, t := range e.TopFolders() {
			out = append(out, davInfo{name: t, dir: true})
		}
	case kindTop:
		for _, f := range e.Folders(d.r.top) {
			out = append(out, davInfo{name: f.Name, dir: true, mod: f.ModTime})
		}
	case kindFolder:
		for _, en := range d.r.folder.Entries {
			out = append(out, davInfo{name: en.Name, size: en.Size, mod: d.r.folder.ModTime, etag: d.r.folder.JobID})
		}
		for _, sc := range e.Sidecars(d.r.folder.JobID) {
			out = append(out, davInfo{name: sc.Name, size: sc.Size, mod: sc.ModTime})
		}
	}
	return out, nil
}

// ---- streamed files -------------------------------------------------------------

// davFile opens the stream on the first read only: PROPFIND and HEAD open
// files too, and those must not start a precache or a "now playing" entry.
type davFile struct {
	fs   davFS
	r    resolved
	info davInfo
	ctx  context.Context
	of   *openFile
	pos  int64
}

func (f *davFile) Stat() (os.FileInfo, error)         { return f.info, nil }
func (f *davFile) Readdir(int) ([]os.FileInfo, error) { return nil, fmt.Errorf("not a directory") }
func (f *davFile) Write([]byte) (int, error)          { return 0, os.ErrPermission }

func (f *davFile) Close() error {
	if f.of != nil {
		f.of.close()
		f.of = nil
	}
	return nil
}

func (f *davFile) Seek(off int64, whence int) (int64, error) {
	switch whence {
	case io.SeekStart:
	case io.SeekCurrent:
		off += f.pos
	case io.SeekEnd:
		off += f.info.size
	default:
		return 0, fmt.Errorf("bad whence")
	}
	if off < 0 {
		return 0, fmt.Errorf("negative offset")
	}
	f.pos = off
	return off, nil
}

func (f *davFile) Read(p []byte) (int, error) {
	if f.pos >= f.info.size {
		return 0, io.EOF
	}
	if f.of == nil {
		of, err := openEntry(f.fs.e, f.fs.log, f.r.folder.JobID, f.r.entry)
		if err != nil {
			return 0, err
		}
		f.of = of
	}
	if rem := f.info.size - f.pos; int64(len(p)) > rem {
		p = p[:rem]
	}
	n, err := f.of.readAt(f.ctx, p, f.pos)
	f.pos += int64(n)
	if err != nil {
		return n, err
	}
	if n == 0 {
		return 0, io.EOF
	}
	return n, nil
}

// ---- file info ------------------------------------------------------------------

type davInfo struct {
	name string
	size int64
	mod  time.Time
	dir  bool
	etag string
}

func (i davInfo) Name() string       { return i.name }
func (i davInfo) Size() int64        { return i.size }
func (i davInfo) ModTime() time.Time { return i.mod }
func (i davInfo) IsDir() bool        { return i.dir }
func (i davInfo) Sys() any           { return nil }
func (i davInfo) Mode() fs.FileMode {
	if i.dir {
		return fs.ModeDir | 0o555
	}
	return 0o444
}

var videoTypes = map[string]string{".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/x-m4v",
	".avi": "video/x-msvideo", ".mov": "video/quicktime", ".ts": "video/mp2t", ".srt": "application/x-subrip"}

// ContentType without sniffing: sniffing would read (and download) the
// start of every file a PROPFIND lists.
func (i davInfo) ContentType(context.Context) (string, error) {
	ext := strings.ToLower(path.Ext(i.name))
	if t, ok := videoTypes[ext]; ok {
		return t, nil
	}
	if t := mime.TypeByExtension(ext); t != "" {
		return t, nil
	}
	return "application/octet-stream", nil
}

// ETag: content never changes for a job's file, so job id + size + name.
func (i davInfo) ETag(context.Context) (string, error) {
	if i.etag == "" {
		return "", webdav.ErrNotImplemented
	}
	return fmt.Sprintf(`"%x-%x-%x"`, i.etag, i.size, i.name), nil
}
