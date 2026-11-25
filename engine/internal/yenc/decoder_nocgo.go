// Adapted from decypharr internal/nntp/yenc (MIT, see engine/NOTICE.md).
//go:build !cgo

package yenc

import (
	"io"
)

// AcquireDecoder returns a Decoder backed by the in-repo pure-Go decoder.
func AcquireDecoder(r io.Reader) *Decoder {
	return acquirePureGoDecoder(r)
}

// ReleaseDecoder is a no-op for the pure-Go backend.
func ReleaseDecoder(dec *Decoder) {}
