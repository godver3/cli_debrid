package archive

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// ZIP sets: stored (uncompressed) members are streamed straight from the
// posted volumes, like cli_mount's ZIP parser. Three layouts:
//   - one .zip;
//   - a byte-split archive (.zip.001, .zip.002, ...): the parts joined are
//     one ZIP;
//   - a spanned archive (.z01, .z02, ..., .zip last): offsets in the
//     central directory are relative to the disk (volume) they name.
// Compressed members (deflate etc.) can't be streamed and are reported
// with ErrCompressed; encrypted ones with ErrEncrypted.

var (
	zipLocalSig = []byte("PK\x03\x04")
	zipSpanSig  = []byte("PK\x07\x08") // first bytes of a spanned set's first disk
	reZipSpan   = regexp.MustCompile(`(?i)\.z(\d{2,3})$`)
	reZipSplit  = regexp.MustCompile(`(?i)\.zip\.(\d{3})$`)
	reZipName   = regexp.MustCompile(`(?i)\.zip$`)
)

// IsZip reports whether head starts a ZIP archive (or its first disk).
func IsZip(head []byte) bool {
	return bytes.HasPrefix(head, zipLocalSig) || bytes.HasPrefix(head, zipSpanSig)
}

// IsZipName reports whether name is a ZIP volume name (.zip, .zip.NNN, .zNN).
func IsZipName(name string) bool {
	return reZipName.MatchString(name) || reZipSplit.MatchString(name) || reZipSpan.MatchString(name)
}

// zipOrder sorts volumes: .zNN by number, then .zip (spanned); .zip.NNN by
// number (split). Reports whether the set is spanned.
func zipOrder(vols []Volume) ([]Volume, bool) {
	ordered := append([]Volume(nil), vols...)
	spanned := false
	key := func(name string) int {
		if m := reZipSpan.FindStringSubmatch(name); m != nil {
			spanned = true
			n, _ := strconv.Atoi(m[1])
			return n
		}
		if m := reZipSplit.FindStringSubmatch(name); m != nil {
			n, _ := strconv.Atoi(m[1])
			return n
		}
		return 1 << 20 // .zip: the last disk of a spanned set
	}
	keys := make(map[string]int, len(ordered))
	for _, v := range ordered {
		keys[v.Name] = key(v.Name)
	}
	sort.SliceStable(ordered, func(a, b int) bool { return keys[ordered[a].Name] < keys[ordered[b].Name] })
	return ordered, spanned
}

// OpenZip lists a ZIP set's members.
func OpenZip(vols []Volume) (*Set, error) {
	if len(vols) == 0 {
		return nil, fmt.Errorf("archive: no volumes")
	}
	ordered, spanned := zipOrder(vols)
	cat := newConcat(ordered)
	cd, entries, err := zipDirectory(cat)
	if err != nil {
		return nil, err
	}
	// Absolute offset of (disk, offset): disk-relative for spanned sets,
	// already absolute for one file or a byte-split set.
	abs := func(disk uint32, off int64) (int64, bool) {
		if !spanned {
			return off, true
		}
		if int(disk) >= len(cat.starts) {
			return 0, false
		}
		return cat.starts[disk] + off, true
	}
	set := &Set{Volumes: ordered}
	r := reader{r: cat, size: cat.size}
	for i := 0; i < int(entries); i++ {
		hdr, err := r.at(cd, 46)
		if err != nil || !bytes.HasPrefix(hdr, []byte("PK\x01\x02")) {
			return nil, fmt.Errorf("%w: zip central directory entry %d", ErrCorrupt, i+1)
		}
		le := binary.LittleEndian
		flags, method := le.Uint16(hdr[8:]), le.Uint16(hdr[10:])
		csize, usize := int64(le.Uint32(hdr[20:])), int64(le.Uint32(hdr[24:]))
		nameLen, extraLen, commentLen := int(le.Uint16(hdr[28:])), int(le.Uint16(hdr[30:])), int(le.Uint16(hdr[32:]))
		disk := uint32(le.Uint16(hdr[34:]))
		local := int64(le.Uint32(hdr[42:]))
		rest, err := r.at(cd+46, nameLen+extraLen)
		if err != nil {
			return nil, fmt.Errorf("%w: zip central directory entry %d", ErrCorrupt, i+1)
		}
		name := string(rest[:nameLen])
		zip64Extra(rest[nameLen:], &usize, &csize, &local, &disk)
		cd += int64(46 + nameLen + extraLen + commentLen)
		if strings.HasSuffix(name, "/") {
			continue
		}
		m := Member{Name: cleanName(strings.ReplaceAll(name, `\`, "/")), Size: usize}
		switch {
		case flags&1 != 0:
			m.Err = fmt.Errorf("%w: %s", ErrEncrypted, m.Name)
		case method != 0:
			m.Err = fmt.Errorf("%w: %s (zip method %d)", ErrCompressed, m.Name, method)
		default:
			start, ok := abs(disk, local)
			var lh []byte
			if ok {
				lh, err = r.at(start, 30)
			}
			if !ok || err != nil || !bytes.HasPrefix(lh, zipLocalSig) {
				m.Err = fmt.Errorf("%w: %s: local header missing", ErrIncomplete, m.Name)
				break
			}
			data := start + 30 + int64(le.Uint16(lh[26:])) + int64(le.Uint16(lh[28:]))
			if data+usize > cat.size || csize != usize {
				m.Err = fmt.Errorf("%w: %s lies past the end of the volumes", ErrIncomplete, m.Name)
				break
			}
			m.Extents = cat.extents(data, usize)
		}
		set.Members = append(set.Members, m)
	}
	return set, nil
}

// zipDirectory finds the central directory (ZIP64 aware): its absolute
// start and entry count. For a spanned set the directory sits on the last
// disk with a disk-relative offset; the EOCD names that disk.
func zipDirectory(cat *concat) (int64, uint64, error) {
	r := reader{r: cat, size: cat.size}
	tail := min(cat.size, 22+65535)
	buf, err := r.at(cat.size-tail, int(tail))
	if err != nil {
		return 0, 0, fmt.Errorf("archive: zip: %w", err)
	}
	i := bytes.LastIndex(buf, []byte("PK\x05\x06"))
	if i < 0 || len(buf)-i < 22 {
		return 0, 0, fmt.Errorf("%w: zip end of central directory not found", ErrIncomplete)
	}
	le := binary.LittleEndian
	eocd := buf[i:]
	cdDisk := uint32(le.Uint16(eocd[6:]))
	entries := uint64(le.Uint16(eocd[10:]))
	cdOff := int64(le.Uint32(eocd[16:]))
	eocdAt := cat.size - tail + int64(i)
	if entries == 0xffff || cdOff == 0xffffffff || cdDisk == 0xffff {
		// ZIP64: the locator sits just before the EOCD.
		loc, err := r.at(eocdAt-20, 20)
		if err != nil || !bytes.HasPrefix(loc, []byte("PK\x06\x07")) {
			return 0, 0, fmt.Errorf("%w: zip64 locator missing", ErrCorrupt)
		}
		z64Disk := le.Uint32(loc[4:])
		z64Off := int64(le.Uint64(loc[8:]))
		if len(cat.starts) > 1 && int(z64Disk) < len(cat.starts) && z64Disk > 0 {
			z64Off += cat.starts[z64Disk]
		}
		rec, err := r.at(z64Off, 56)
		if err != nil || !bytes.HasPrefix(rec, []byte("PK\x06\x06")) {
			return 0, 0, fmt.Errorf("%w: zip64 end record missing", ErrCorrupt)
		}
		cdDisk = le.Uint32(rec[20:])
		entries = le.Uint64(rec[32:])
		cdOff = int64(le.Uint64(rec[48:]))
	}
	// A spanned set's directory offset is relative to its disk.
	if cdDisk > 0 && int(cdDisk) < len(cat.starts) {
		cdOff += cat.starts[cdDisk]
	}
	if cdOff < 0 || cdOff >= cat.size || entries > 1<<20 {
		return 0, 0, fmt.Errorf("%w: zip central directory out of range", ErrCorrupt)
	}
	return cdOff, entries, nil
}

// zip64Extra replaces 0xffffffff fields with their ZIP64 extra values (in
// the order the format lists them: size, compressed size, offset, disk).
func zip64Extra(extra []byte, usize, csize, local *int64, disk *uint32) {
	le := binary.LittleEndian
	for len(extra) >= 4 {
		id, n := le.Uint16(extra), int(le.Uint16(extra[2:]))
		if 4+n > len(extra) {
			return
		}
		if id == 0x0001 {
			f := extra[4 : 4+n]
			next := func() (uint64, bool) {
				if len(f) < 8 {
					return 0, false
				}
				v := le.Uint64(f)
				f = f[8:]
				return v, true
			}
			if *usize == 0xffffffff {
				if v, ok := next(); ok {
					*usize = int64(v)
				}
			}
			if *csize == 0xffffffff {
				if v, ok := next(); ok {
					*csize = int64(v)
				}
			}
			if *local == 0xffffffff {
				if v, ok := next(); ok {
					*local = int64(v)
				}
			}
			if *disk == 0xffff && len(f) >= 4 {
				*disk = le.Uint32(f)
			}
			return
		}
		extra = extra[4+n:]
	}
}
