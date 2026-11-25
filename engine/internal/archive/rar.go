// Package archive maps files stored inside RAR and 7z sets to byte ranges
// of the posted volumes, so they can be streamed without extracting.
//
// Only stored (uncompressed) members can be streamed; that is how nearly
// every usenet video release is packed. RAR5 and 7z sets protected with a
// password (data only, or headers too) stream when the password is known;
// RAR4 encryption is not supported. Anything that can't be streamed is
// reported with a clear error rather than served as garbage.
//
// Header layouts follow the RAR 4 and RAR 5 technical notes; volume-number
// handling mirrors decypharr's parser (MIT, see engine/NOTICE.md).
package archive

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"strings"
)

var (
	rar4Sig = []byte("Rar!\x1a\x07\x00")
	rar5Sig = []byte("Rar!\x1a\x07\x01\x00")

	// ErrNotRAR means the data does not start with a RAR signature.
	ErrNotRAR = errors.New("archive: not a RAR volume")
	// ErrEncrypted means headers or data are password protected.
	ErrEncrypted = errors.New("archive: encrypted archive")
	// ErrCompressed means a member is compressed and cannot be streamed.
	ErrCompressed = errors.New("archive: compressed member (not stored)")
	// ErrIncomplete means a member's parts do not add up to its size
	// (a volume is missing or the set is broken).
	ErrIncomplete = errors.New("archive: member incomplete")
	// ErrCorrupt means a header could not be parsed.
	ErrCorrupt = errors.New("archive: corrupt header")
)

// Version is the RAR format generation.
type Version int

const (
	RAR4 Version = 4
	RAR5 Version = 5
)

// DetectRAR reports the RAR version from a volume's first bytes (0 if not RAR).
func DetectRAR(head []byte) Version {
	switch {
	case bytes.HasPrefix(head, rar5Sig):
		return RAR5
	case bytes.HasPrefix(head, rar4Sig):
		return RAR4
	}
	return 0
}

// Part is one member's slice of data inside one volume.
type Part struct {
	Name      string
	Offset    int64 // where the data starts in the volume
	Length    int64 // bytes of member data in this volume
	Unpacked  int64 // whole member size
	Stored    bool
	Dir       bool
	Encrypted bool
	// Crypt is a RAR5 file's encryption record (nil for RAR4, whose
	// encryption isn't supported, and for unencrypted files).
	Crypt       *rar5Crypt
	SplitBefore bool // continues from the previous volume
	SplitAfter  bool // continues in the next volume
}

// VolumeInfo is what one volume's headers say.
type VolumeInfo struct {
	Version Version
	// Number is the 0-based volume number from the headers, -1 if unknown.
	Number int
	First  bool // headers mark it as the first volume
	Multi  bool // part of a multi-volume set
	// HeadersEncrypted: the volume's headers were encrypted (RAR5 -hp).
	HeadersEncrypted bool
	Parts            []Part
}

// reader reads header bytes at absolute offsets with bounds checks.
type reader struct {
	r        io.ReaderAt
	size     int64
	password string
}

func (rd reader) at(off int64, n int) ([]byte, error) {
	if off < 0 || n < 0 || off+int64(n) > rd.size {
		return nil, fmt.Errorf("%w: header at %d+%d past volume end %d", ErrCorrupt, off, n, rd.size)
	}
	b := make([]byte, n)
	if _, err := rd.r.ReadAt(b, off); err != nil && !(errors.Is(err, io.EOF) && off+int64(n) == rd.size) {
		return nil, err
	}
	return b, nil
}

// ParseVolume reads one volume's headers. Only header bytes are read: for
// stored data that is the start of the volume and the end-of-archive block.
func ParseVolume(r io.ReaderAt, size int64) (*VolumeInfo, error) {
	return ParseVolumePassword(r, size, "")
}

// ParseVolumePassword is ParseVolume for sets that may be password
// protected: the password opens RAR5 encrypted headers.
func ParseVolumePassword(r io.ReaderAt, size int64, password string) (*VolumeInfo, error) {
	rd := reader{r: r, size: size, password: password}
	head, err := rd.at(0, min(8, int(size)))
	if err != nil {
		return nil, err
	}
	switch DetectRAR(head) {
	case RAR5:
		return parseRAR5(rd)
	case RAR4:
		return parseRAR4(rd)
	}
	return nil, ErrNotRAR
}

// ---- RAR 4 -----------------------------------------------------------------

const (
	rar4Main = 0x73
	rar4File = 0x74
	rar4End  = 0x7b

	rar4LongBlock = 0x8000

	rar4MainVolume    = 0x0001
	rar4MainPassword  = 0x0080 // block headers encrypted
	rar4MainFirstVol  = 0x0100
	rar4FileSplitBef  = 0x0001
	rar4FileSplitAft  = 0x0002
	rar4FilePassword  = 0x0004
	rar4FileDirMask   = 0x00e0
	rar4FileLarge     = 0x0100
	rar4FileUnicode   = 0x0200
	rar4EndDataCRC    = 0x0002
	rar4EndVolNumber  = 0x0008
	rar4MethodStored  = 0x30
	rar4FileFixedSize = 32 // base 7 + fixed file fields 25
)

func parseRAR4(rd reader) (*VolumeInfo, error) {
	v := &VolumeInfo{Version: RAR4, Number: -1}
	off := int64(len(rar4Sig))
	for off+7 <= rd.size {
		base, err := rd.at(off, 7)
		if err != nil {
			return nil, err
		}
		typ := base[2]
		flags := binary.LittleEndian.Uint16(base[3:5])
		hsize := int64(binary.LittleEndian.Uint16(base[5:7]))
		if hsize < 7 {
			return nil, fmt.Errorf("%w: RAR4 block size %d at %d", ErrCorrupt, hsize, off)
		}
		hdr, err := rd.at(off, int(hsize))
		if err != nil {
			return nil, err
		}
		var dataLen int64
		switch typ {
		case rar4Main:
			v.Multi = flags&rar4MainVolume != 0
			v.First = flags&rar4MainFirstVol != 0
			if flags&rar4MainPassword != 0 {
				return nil, fmt.Errorf("%w: RAR4 headers are encrypted (RAR4 encryption is not supported)", ErrEncrypted)
			}
		case rar4File:
			p, n, err := parseRAR4File(hdr, flags)
			if err != nil {
				return nil, err
			}
			p.Offset = off + hsize
			v.Parts = append(v.Parts, p)
			dataLen = n
		case rar4End:
			v.Number = rar4EndVolumeNumber(flags, hdr[7:])
			return finishRAR4(v), nil
		default:
			if flags&rar4LongBlock != 0 {
				if len(hdr) < 11 {
					return nil, fmt.Errorf("%w: RAR4 long block too short", ErrCorrupt)
				}
				dataLen = int64(binary.LittleEndian.Uint32(hdr[7:11]))
			}
		}
		off += hsize + dataLen
	}
	return finishRAR4(v), nil
}

// finishRAR4 fills in what the end block could not: a first volume with
// no explicit number is volume 0.
func finishRAR4(v *VolumeInfo) *VolumeInfo {
	if v.Number < 0 && v.First {
		v.Number = 0
	}
	return v
}

// rar4EndVolumeNumber reads the volume number field of an end-of-archive
// block (data after the 7-byte base), -1 if absent or truncated.
func rar4EndVolumeNumber(flags uint16, data []byte) int {
	if flags&rar4EndVolNumber == 0 {
		return -1
	}
	pos := 0
	if flags&rar4EndDataCRC != 0 {
		pos = 4
	}
	if len(data) < pos+2 {
		return -1
	}
	return int(binary.LittleEndian.Uint16(data[pos:]))
}

func parseRAR4File(hdr []byte, flags uint16) (Part, int64, error) {
	if len(hdr) < rar4FileFixedSize {
		return Part{}, 0, fmt.Errorf("%w: RAR4 file header too short", ErrCorrupt)
	}
	le := binary.LittleEndian
	pack := int64(le.Uint32(hdr[7:11]))
	unp := int64(le.Uint32(hdr[11:15]))
	method := hdr[25]
	nameSize := int(le.Uint16(hdr[26:28]))
	pos := rar4FileFixedSize
	if flags&rar4FileLarge != 0 {
		if len(hdr) < pos+8 {
			return Part{}, 0, fmt.Errorf("%w: RAR4 large file header too short", ErrCorrupt)
		}
		pack |= int64(le.Uint32(hdr[pos:])) << 32
		unp |= int64(le.Uint32(hdr[pos+4:])) << 32
		pos += 8
	}
	if len(hdr) < pos+nameSize {
		return Part{}, 0, fmt.Errorf("%w: RAR4 file name past header", ErrCorrupt)
	}
	name := hdr[pos : pos+nameSize]
	if flags&rar4FileUnicode != 0 {
		// Unicode names are "<ascii>\x00<encoded unicode>"; the ASCII form
		// is what nearly every poster uses. Prefer it when present.
		if i := bytes.IndexByte(name, 0); i >= 0 {
			name = name[:i]
		}
	}
	return Part{
		Name:        cleanName(string(name)),
		Length:      pack,
		Unpacked:    unp,
		Stored:      method == rar4MethodStored,
		Dir:         flags&rar4FileDirMask == rar4FileDirMask,
		Encrypted:   flags&rar4FilePassword != 0,
		SplitBefore: flags&rar4FileSplitBef != 0,
		SplitAfter:  flags&rar4FileSplitAft != 0,
	}, pack, nil
}

// ---- RAR 5 -----------------------------------------------------------------

const (
	rar5Main       = 1
	rar5File       = 2
	rar5Service    = 3
	rar5Encryption = 4
	rar5End        = 5

	rar5HdrExtra     = 0x01
	rar5HdrData      = 0x02
	rar5HdrSplitBef  = 0x08
	rar5HdrSplitAft  = 0x10
	rar5MainVolume   = 0x01
	rar5MainVolNum   = 0x02
	rar5FileDir      = 0x01
	rar5FileTime     = 0x02
	rar5FileCRC      = 0x04
	rar5ExtraCrypt   = 0x01
	rar5MaxHeaderLen = 2 << 20 // sanity bound
)

// vint decodes a RAR5 variable-length integer, returning value and length
// (0 length on truncation or overflow).
func vint(b []byte) (uint64, int) {
	var v uint64
	for i := 0; i < len(b) && i < 10; i++ {
		v |= uint64(b[i]&0x7f) << (7 * i)
		if b[i]&0x80 == 0 {
			return v, i + 1
		}
	}
	return 0, 0
}

// fields walks vints in a header body.
type fields struct {
	b   []byte
	pos int
	err error
}

func (f *fields) next() uint64 {
	if f.err != nil {
		return 0
	}
	v, n := vint(f.b[f.pos:])
	if n == 0 {
		f.err = fmt.Errorf("%w: RAR5 vint truncated", ErrCorrupt)
		return 0
	}
	f.pos += n
	return v
}

func (f *fields) u32() uint32 {
	if f.err != nil || f.pos+4 > len(f.b) {
		f.err = fmt.Errorf("%w: RAR5 field truncated", ErrCorrupt)
		return 0
	}
	v := binary.LittleEndian.Uint32(f.b[f.pos:])
	f.pos += 4
	return v
}

func (f *fields) bytes(n int) []byte {
	if f.err != nil || n < 0 || f.pos+n > len(f.b) {
		f.err = fmt.Errorf("%w: RAR5 field truncated", ErrCorrupt)
		return nil
	}
	v := f.b[f.pos : f.pos+n]
	f.pos += n
	return v
}

// rar5MainVolumeNumber reads the volume number from a main header body
// that starts at the archive flags: 0 for the first volume (which omits the
// field), -1 if the field should be there but is truncated.
func rar5MainVolumeNumber(body []byte) int {
	if len(body) == 0 {
		return -1
	}
	f := &fields{b: body}
	flags := f.next()
	if f.err != nil {
		return -1
	}
	if flags&rar5MainVolNum == 0 {
		return 0
	}
	n := f.next()
	if f.err != nil {
		return -1
	}
	return int(n)
}

func parseRAR5(rd reader) (*VolumeInfo, error) {
	v := &VolumeInfo{Version: RAR5, Number: -1}
	off := int64(len(rar5Sig))
	var hdrKey []byte // set once an encryption header was opened
	for off+5 < rd.size {
		hdr, disk, err := readRAR5Header(rd, off, hdrKey)
		if err != nil {
			return nil, err
		}
		f := &fields{b: hdr, pos: 4}
		f.next() // header size, already checked
		typ := f.next()
		hflags := f.next()
		var extraSize, dataSize uint64
		if hflags&rar5HdrExtra != 0 {
			extraSize = f.next()
		}
		if hflags&rar5HdrData != 0 {
			dataSize = f.next()
		}
		if f.err != nil {
			return nil, f.err
		}
		bodyEnd := len(hdr) - int(extraSize)
		if bodyEnd < f.pos {
			return nil, fmt.Errorf("%w: RAR5 extra area larger than header", ErrCorrupt)
		}
		switch typ {
		case rar5Main:
			body := hdr[f.pos:bodyEnd]
			af := &fields{b: body}
			flags := af.next()
			v.Multi = flags&rar5MainVolume != 0
			v.Number = rar5MainVolumeNumber(body)
			v.First = v.Number == 0
		case rar5Encryption:
			if hdrKey != nil {
				return nil, fmt.Errorf("%w: second RAR5 encryption header", ErrCorrupt)
			}
			c, err := parseRAR5Crypt(hdr[f.pos:bodyEnd], false)
			if err != nil {
				return nil, err
			}
			key, err := c.open(rd.password)
			if err != nil {
				return nil, fmt.Errorf("RAR5 headers are encrypted: %w", err)
			}
			hdrKey = key
			v.HeadersEncrypted = true
		case rar5File:
			p, err := parseRAR5File(&fields{b: hdr[:bodyEnd], pos: f.pos}, hdr[bodyEnd:], hflags)
			if err != nil {
				return nil, err
			}
			p.Offset = off + disk
			p.Length = int64(dataSize)
			v.Parts = append(v.Parts, p)
		case rar5End:
			return v, nil
		}
		off += disk + int64(dataSize)
	}
	return v, nil
}

// readRAR5Header returns the header at off (CRC32 + size vint + body) and
// how many bytes it takes on disk. With key set, headers are encrypted:
// each is a 16-byte IV followed by the header padded to whole AES blocks.
func readRAR5Header(rd reader, off int64, key []byte) ([]byte, int64, error) {
	if key == nil {
		// CRC32 (4) + header size vint (<= 3 bytes for any sane header).
		pre, err := rd.at(off, int(min(int64(7), rd.size-off)))
		if err != nil {
			return nil, 0, err
		}
		hsize, n := vint(pre[4:])
		if n == 0 || hsize == 0 || hsize > rar5MaxHeaderLen {
			return nil, 0, fmt.Errorf("%w: RAR5 header size at %d", ErrCorrupt, off)
		}
		total := int64(4+n) + int64(hsize)
		hdr, err := rd.at(off, int(total))
		return hdr, total, err
	}
	const bs = 16
	first, err := rd.at(off, 2*bs)
	if err != nil {
		return nil, 0, err
	}
	iv := first[:bs]
	blk := append([]byte(nil), first[bs:]...)
	if err := decryptCBC(key, iv, blk); err != nil {
		return nil, 0, err
	}
	hsize, n := vint(blk[4:])
	if n == 0 || hsize == 0 || hsize > rar5MaxHeaderLen {
		return nil, 0, fmt.Errorf("%w: encrypted RAR5 header size at %d (wrong password?)", ErrCorrupt, off)
	}
	total := int64(4+n) + int64(hsize)
	padded := roundBlock(total)
	raw, err := rd.at(off+bs, int(padded))
	if err != nil {
		return nil, 0, err
	}
	if err := decryptCBC(key, iv, raw); err != nil {
		return nil, 0, err
	}
	if crc32.ChecksumIEEE(raw[4:total]) != binary.LittleEndian.Uint32(raw[:4]) {
		return nil, 0, fmt.Errorf("%w: encrypted RAR5 header at %d fails its CRC (wrong password?)", ErrCorrupt, off)
	}
	return raw[:total], bs + padded, nil
}

func parseRAR5File(f *fields, extra []byte, hflags uint64) (Part, error) {
	fileFlags := f.next()
	unp := f.next()
	f.next() // attributes
	if fileFlags&rar5FileTime != 0 {
		f.u32()
	}
	if fileFlags&rar5FileCRC != 0 {
		f.u32()
	}
	comp := f.next()
	f.next() // host OS
	nameLen := f.next()
	name := f.bytes(int(nameLen))
	if f.err != nil {
		return Part{}, f.err
	}
	p := Part{
		Name:        cleanName(string(name)),
		Unpacked:    int64(unp),
		Stored:      (comp>>7)&0x7 == 0,
		Dir:         fileFlags&rar5FileDir != 0,
		SplitBefore: hflags&rar5HdrSplitBef != 0,
		SplitAfter:  hflags&rar5HdrSplitAft != 0,
	}
	if rec := rar5ExtraCryptRecord(extra); rec != nil {
		p.Encrypted = true
		c, err := parseRAR5Crypt(rec, true)
		if err != nil {
			return Part{}, err
		}
		p.Crypt = c
	}
	return p, nil
}

// rar5ExtraCryptRecord returns the body (after the type) of a file
// header's encryption record, nil if there is none.
func rar5ExtraCryptRecord(extra []byte) []byte {
	for len(extra) > 0 {
		size, n := vint(extra)
		if n == 0 || size == 0 || int(size) > len(extra)-n {
			return nil
		}
		rec := extra[n : n+int(size)]
		if typ, m := vint(rec); m > 0 && typ == rar5ExtraCrypt {
			return rec[m:]
		}
		extra = extra[n+int(size):]
	}
	return nil
}

// cleanName normalises member paths: forward slashes, no leading slash.
func cleanName(s string) string {
	s = strings.ReplaceAll(s, "\\", "/")
	return strings.TrimLeft(s, "/")
}
