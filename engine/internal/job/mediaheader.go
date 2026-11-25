package job

import (
	"bytes"
	"encoding/binary"
	"path"
	"strings"
)

// Ported from cli_mount (decypharr pkg/usenet/parser/media_header.go, MIT).

// mediaHeaderLen is how many bytes of a file's start identify its container.
const mediaHeaderLen = 16

// mediaHeaderValid reports whether head looks like the start of a file with
// the given name's container. Unknown extensions (and MPEG, whose start can
// legitimately vary) are always accepted.
func mediaHeaderValid(name string, head []byte) bool {
	switch strings.ToLower(path.Ext(name)) {
	case ".mkv", ".webm":
		return bytes.HasPrefix(head, []byte{0x1A, 0x45, 0xDF, 0xA3})
	case ".mp4", ".m4v", ".mov":
		return isoBoxHeader(head)
	case ".avi":
		return bytes.HasPrefix(head, []byte("RIFF"))
	}
	return true
}

// isoBoxHeader: a plausible ISO-BMFF box (32-bit size 0, 1 or >= 8, then a
// four-character type of printable characters).
func isoBoxHeader(head []byte) bool {
	if len(head) < 8 {
		return false
	}
	size := binary.BigEndian.Uint32(head[:4])
	if size != 0 && size != 1 && size < 8 {
		return false
	}
	for _, b := range head[4:8] {
		alnum := (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
		if !alnum && b != ' ' && b != 0xA9 {
			return false
		}
	}
	return true
}

// MediaHeaderLen is how many bytes MediaHeaderValid needs.
const MediaHeaderLen = mediaHeaderLen

// MediaHeaderValid is mediaHeaderValid for health checks.
func MediaHeaderValid(name string, head []byte) bool { return mediaHeaderValid(name, head) }
