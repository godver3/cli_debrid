// Package par2 reads file descriptions from PAR2 data to recover the real
// names of obfuscated uploads: each description pairs a file's name with
// the MD5 of its first 16 KiB and its length.
//
// Packet parsing follows decypharr's parser (MIT, see engine/NOTICE.md).
package par2

import (
	"bytes"
	"crypto/md5"
	"encoding/binary"
)

const (
	magic        = "PAR2\x00PKT"
	fileDescType = "PAR 2.0\x00FileDesc"
	headerLen    = 64 // magic 8, length 8, packet MD5 16, set id 16, type 16
	// HashLen is how much of a file the 16k hash covers.
	HashLen = 16 * 1024
)

// FileDesc is one file description packet.
type FileDesc struct {
	ID      [16]byte
	Hash16k [16]byte
	Length  uint64
	Name    string
}

// Parse returns the unique file descriptions found anywhere in data (PAR2
// repeats them in every volume; junk between packets is skipped).
func Parse(data []byte) []FileDesc {
	var out []FileDesc
	seen := map[[16]byte]bool{}
	for off := 0; off+headerLen <= len(data); {
		i := bytes.Index(data[off:], []byte(magic))
		if i < 0 {
			break
		}
		off += i
		if off+headerLen > len(data) {
			break
		}
		n := binary.LittleEndian.Uint64(data[off+8 : off+16])
		if n < headerLen || n%4 != 0 || n > uint64(len(data)-off) {
			off++ // not a real packet header (or truncated): keep scanning
			continue
		}
		pkt := data[off : off+int(n)]
		if string(pkt[48:64]) == fileDescType && len(pkt) >= headerLen+56 {
			body := pkt[headerLen:]
			var fd FileDesc
			copy(fd.ID[:], body[0:16])
			copy(fd.Hash16k[:], body[32:48])
			fd.Length = binary.LittleEndian.Uint64(body[48:56])
			name := body[56:]
			if z := bytes.IndexByte(name, 0); z >= 0 {
				name = name[:z]
			}
			if len(name) > 0 && !seen[fd.ID] {
				seen[fd.ID] = true
				fd.Name = string(name)
				out = append(out, fd)
			}
		}
		off += int(n)
	}
	return out
}

// Hash16k is the MD5 of a file's first 16 KiB (the whole file if shorter).
func Hash16k(head []byte) [16]byte {
	if len(head) > HashLen {
		head = head[:HashLen]
	}
	return md5.Sum(head)
}

// Candidate is a posted file to name: its first bytes and decoded size
// (Size 0 = unknown).
type Candidate struct {
	Head []byte
	Size int64
}

// Match returns candidate index -> real name. A candidate matches when its
// 16k hash equals a description's and, if both are known, the lengths
// agree. Hashes shared by several descriptions with different names are
// ambiguous and skipped.
func Match(descs []FileDesc, cands []Candidate) map[int]string {
	byHash := map[[16]byte][]FileDesc{}
	for _, d := range descs {
		byHash[d.Hash16k] = append(byHash[d.Hash16k], d)
	}
	out := map[int]string{}
	for i, c := range cands {
		if len(c.Head) < HashLen && (c.Size == 0 || int64(len(c.Head)) < c.Size) {
			continue // not enough bytes to hash
		}
		var hits []FileDesc
		for _, d := range byHash[Hash16k(c.Head)] {
			if c.Size == 0 || d.Length == uint64(c.Size) {
				hits = append(hits, d)
			}
		}
		if len(hits) == 0 {
			continue
		}
		name := hits[0].Name
		ambiguous := false
		for _, h := range hits[1:] {
			if h.Name != name {
				ambiguous = true
			}
		}
		if !ambiguous {
			out[i] = name
		}
	}
	return out
}
