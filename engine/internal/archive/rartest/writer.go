// Package rartest writes stored multi-volume RAR4/RAR5 sets for tests.
// Its output is checked against 7z (an independent RAR reader) in the
// archive package's TestFixturesExtractWith7z.
package rartest

import (
	"bytes"
	"encoding/binary"
	"hash/crc32"
)

// Format constants, kept separate from package archive on purpose so the
// writer does not share the parser's definitions.
const (
	RAR4 = 4
	RAR5 = 5

	rar4Main         = 0x73
	rar4File         = 0x74
	rar4End          = 0x7b
	rar4LongBlock    = 0x8000
	rar4MainVolume   = 0x0001
	rar4MainFirstVol = 0x0100
	rar4FileSplitBef = 0x0001
	rar4FileSplitAft = 0x0002
	rar4EndVolNumber = 0x0008
	rar4MethodStored = 0x30
	rar5Main         = 1
	rar5File         = 2
	rar5End          = 5
	rar5HdrData      = 0x02
	rar5HdrSplitBef  = 0x08
	rar5HdrSplitAft  = 0x10
	rar5MainVolume   = 0x01
	rar5MainVolNum   = 0x02
	rar5FileCRC      = 0x04
)

var (
	rar4Sig = []byte("Rar!\x1a\x07\x00")
	rar5Sig = []byte("Rar!\x1a\x07\x01\x00")
)

// Member is a file to store.
type Member struct {
	Name string
	Data []byte
}

// Write stores members (format RAR4 or RAR5) in volumes holding at most
// volData bytes of member data each and returns the volume images.
func Write(v int, members []Member, volData int) [][]byte {
	type piece struct {
		m                       int
		from, to                int
		splitBefore, splitAfter bool
	}
	// Lay pieces into volumes.
	var vols [][]piece
	cur, room := []piece{}, volData
	for mi, m := range members {
		from := 0
		for {
			if room == 0 { // volume full: never leave an empty piece in it
				vols = append(vols, cur)
				cur, room = nil, volData
			}
			take := min(room, len(m.Data)-from)
			p := piece{m: mi, from: from, to: from + take, splitBefore: from > 0}
			from += take
			room -= take
			p.splitAfter = from < len(m.Data)
			cur = append(cur, p)
			if from == len(m.Data) {
				break
			}
			vols = append(vols, cur)
			cur, room = nil, volData
		}
	}
	vols = append(vols, cur)

	out := make([][]byte, len(vols))
	for vi, pieces := range vols {
		var b bytes.Buffer
		last := vi == len(vols)-1
		multi := len(vols) > 1
		if v == RAR4 {
			b.Write(rar4Sig)
			writeRAR4Main(&b, multi, vi == 0)
			for _, p := range pieces {
				m := members[p.m]
				part := m.Data[p.from:p.to]
				crc := crc32.ChecksumIEEE(part)
				if !p.splitAfter {
					crc = crc32.ChecksumIEEE(m.Data) // last part carries the whole file's CRC
				}
				writeRAR4File(&b, m.Name, part, len(m.Data), crc, p.splitBefore, p.splitAfter)
			}
			writeRAR4End(&b, !last, vi)
		} else {
			b.Write(rar5Sig)
			writeRAR5Main(&b, multi, vi)
			for _, p := range pieces {
				m := members[p.m]
				part := m.Data[p.from:p.to]
				crc := crc32.ChecksumIEEE(part)
				if !p.splitAfter {
					crc = crc32.ChecksumIEEE(m.Data)
				}
				writeRAR5File(&b, m.Name, part, len(m.Data), crc, p.splitBefore, p.splitAfter)
			}
			writeRAR5End(&b, !last)
		}
		out[vi] = b.Bytes()
	}
	return out
}

func rar4Block(b *bytes.Buffer, typ byte, flags uint16, body []byte) {
	h := make([]byte, 7, 7+len(body))
	h[2] = typ
	binary.LittleEndian.PutUint16(h[3:], flags)
	binary.LittleEndian.PutUint16(h[5:], uint16(7+len(body)))
	h = append(h, body...)
	binary.LittleEndian.PutUint16(h[0:], uint16(crc32.ChecksumIEEE(h[2:])&0xffff))
	b.Write(h)
}

func writeRAR4Main(b *bytes.Buffer, multi, first bool) {
	var flags uint16
	if multi {
		flags |= rar4MainVolume | 0x0010 // new volume naming (partN.rar)
		if first {
			flags |= rar4MainFirstVol
		}
	}
	rar4Block(b, rar4Main, flags, make([]byte, 6))
}

func writeRAR4File(b *bytes.Buffer, name string, part []byte, unp int, crc uint32, before, after bool) {
	flags := uint16(rar4LongBlock)
	if before {
		flags |= rar4FileSplitBef
	}
	if after {
		flags |= rar4FileSplitAft
	}
	le := binary.LittleEndian
	body := make([]byte, 25, 25+len(name))
	le.PutUint32(body[0:], uint32(len(part)))
	le.PutUint32(body[4:], uint32(unp))
	body[8] = 2                         // host OS: Windows
	le.PutUint32(body[9:], crc)         // file CRC
	le.PutUint32(body[13:], 0x5a210000) // DOS time
	body[17] = 29                       // version needed: 2.9
	body[18] = rar4MethodStored
	le.PutUint16(body[19:], uint16(len(name)))
	le.PutUint32(body[21:], 0x20) // archive attribute
	body = append(body, name...)
	rar4Block(b, rar4File, flags, body)
	b.Write(part)
}

func writeRAR4End(b *bytes.Buffer, more bool, vol int) {
	flags := uint16(rar4EndVolNumber)
	if more {
		flags |= 0x0001 // next volume follows
	}
	body := make([]byte, 2)
	binary.LittleEndian.PutUint16(body, uint16(vol))
	rar4Block(b, rar4End, flags, body)
}

func putVint(b []byte, v uint64) []byte {
	for v >= 0x80 {
		b = append(b, byte(v)|0x80)
		v >>= 7
	}
	return append(b, byte(v))
}

// rar5Block writes CRC32 + size vint + (type, flags, [data size], body).
func rar5Block(b *bytes.Buffer, typ, flags uint64, dataSize int, body []byte) {
	var h []byte
	h = putVint(h, typ)
	h = putVint(h, flags)
	if flags&rar5HdrData != 0 {
		h = putVint(h, uint64(dataSize))
	}
	h = append(h, body...)
	sized := putVint(nil, uint64(len(h)))
	sized = append(sized, h...)
	crc := make([]byte, 4)
	binary.LittleEndian.PutUint32(crc, crc32.ChecksumIEEE(sized))
	b.Write(crc)
	b.Write(sized)
}

func writeRAR5Main(b *bytes.Buffer, multi bool, vol int) {
	var flags uint64
	var body []byte
	if multi {
		flags |= rar5MainVolume
		if vol > 0 {
			flags |= rar5MainVolNum
		}
	}
	body = putVint(body, flags)
	if flags&rar5MainVolNum != 0 {
		body = putVint(body, uint64(vol))
	}
	rar5Block(b, rar5Main, 0, 0, body)
}

func writeRAR5File(b *bytes.Buffer, name string, part []byte, unp int, crc uint32, before, after bool) {
	hflags := uint64(rar5HdrData)
	if before {
		hflags |= rar5HdrSplitBef
	}
	if after {
		hflags |= rar5HdrSplitAft
	}
	var body []byte
	body = putVint(body, rar5FileCRC) // file flags: CRC present
	body = putVint(body, uint64(unp))
	body = putVint(body, 0x20) // attributes
	c := make([]byte, 4)
	binary.LittleEndian.PutUint32(c, crc)
	body = append(body, c...)
	body = putVint(body, 0) // compression: version 0, method 0 (store)
	body = putVint(body, 0) // host OS: Windows
	body = putVint(body, uint64(len(name)))
	body = append(body, name...)
	rar5Block(b, rar5File, hflags, len(part), body)
	b.Write(part)
}

func writeRAR5End(b *bytes.Buffer, more bool) {
	var flags uint64
	if more {
		flags = 1
	}
	rar5Block(b, rar5End, 0, 0, putVint(nil, flags))
}
