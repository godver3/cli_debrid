package mount

// Mounted is a live mount (go-fuse on Linux, WinFsp on Windows).
type Mounted interface {
	Unmount() error
}

// Options are the mount's owner and permissions.
type Options struct {
	AllowOther bool // let other users (Plex) read the mount; needs user_allow_other
	UID, GID   uint32
	Umask      uint32 // permission bits removed from the read-only modes (e.g. 0o027)
	Debug      bool
}
