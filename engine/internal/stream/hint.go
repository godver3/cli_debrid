package stream

import "context"

// Per-reader read-ahead. A mount handle tells the reader, through the read's
// context, how far ahead it is worth fetching for THIS reader and when to
// stop: a scanner (Bazarr's or Plex's ffprobe) reads a few hundred KB at
// the head and the tail and closes the file, so the full read-ahead it used
// to start at both ends (16 MB of articles each, 32 MB of debrid stream)
// was downloaded for nobody, and enough probes in a row filled the line and
// made real playback buffer on start. The mount grows the window as a
// handle keeps reading sequentially, so playback reaches the full
// read-ahead within a few MB, and cancels the handle's prefetches when it
// closes. Reads without a hint (pre-cache, prewarm, tests) keep the file's
// configured read-ahead.

type hintKey struct{}

// ReadHint is attached to a read's context by the mount.
type ReadHint struct {
	// Ahead caps the read-ahead started by this read, in bytes (0 = the
	// file's configured read-ahead). It never raises the configured one.
	Ahead int64
	// Owner ends the read-ahead this read starts (the handle closed).
	// nil = it runs to completion.
	Owner context.Context
}

// WithReadHint returns ctx carrying h.
func WithReadHint(ctx context.Context, h ReadHint) context.Context {
	return context.WithValue(ctx, hintKey{}, h)
}

func hintOf(ctx context.Context) (ReadHint, bool) {
	h, ok := ctx.Value(hintKey{}).(ReadHint)
	return h, ok
}

// aheadFor returns how many units (articles or chunks of unit bytes) to read
// ahead after a read with ctx, given the file's configured n, and the
// context the read-ahead runs under.
func aheadFor(ctx context.Context, n int, unit int64) (int, context.Context) {
	h, ok := hintOf(ctx)
	if !ok {
		return n, context.Background()
	}
	owner := h.Owner
	if owner == nil {
		owner = context.Background()
	}
	if h.Ahead > 0 && unit > 0 {
		n = min(n, max(1, int((h.Ahead+unit-1)/unit)))
	}
	return n, owner
}

// aheadBytesFor caps a byte read-ahead (debrid streams) by the hint.
func aheadBytesFor(ctx context.Context, n int64) int64 {
	if h, ok := hintOf(ctx); ok && h.Ahead > 0 {
		return min(n, h.Ahead)
	}
	return n
}
