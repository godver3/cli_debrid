//go:build !linux

package mount

import "log/slog"

// LazyUnmount: only Linux has lazy FUSE unmounts.
func LazyUnmount(path string) error { return nil }

// ClearStale: stale-mount cleanup is Linux-only.
func ClearStale(path string, log *slog.Logger) {}
