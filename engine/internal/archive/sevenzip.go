package archive

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/javi11/sevenzip"
)

var sevenZipSig = []byte{'7', 'z', 0xBC, 0xAF, 0x27, 0x1C}

// IsSevenZip reports whether a first volume starts with the 7z signature.
// Later volumes of a split set (.7z.002...) are raw continuation bytes.
func IsSevenZip(head []byte) bool { return bytes.HasPrefix(head, sevenZipSig) }

// OpenSevenZip lists a (possibly split) 7z archive. Split 7z volumes are a
// plain byte split of one archive, so they are joined in name order
// (.7z.001, .7z.002, ...) and the members' absolute offsets mapped back
// onto the volumes.
func OpenSevenZip(vols []Volume) (*Set, error) { return OpenSevenZipPassword(vols, "") }

// OpenSevenZipPassword is OpenSevenZip for archives that may be password
// protected. Stored members of an encrypted folder are streamed by
// decrypting the folder's AES-CBC stream from its start; with encrypted
// headers the password is needed to list anything at all.
func OpenSevenZipPassword(vols []Volume, password string) (*Set, error) {
	if len(vols) == 0 {
		return nil, fmt.Errorf("archive: no volumes")
	}
	ordered := append([]Volume(nil), vols...)
	sort.SliceStable(ordered, func(a, b int) bool {
		return volumeNameKey(ordered[a].Name) < volumeNameKey(ordered[b].Name)
	})
	cat := newConcat(ordered)
	r, err := sevenzip.NewReaderWithPassword(cat, cat.size, password)
	if err != nil {
		if headersEncrypted(cat) {
			if password == "" {
				return nil, fmt.Errorf("%w: 7z headers are encrypted, password needed", ErrEncrypted)
			}
			return nil, fmt.Errorf("archive: 7z headers are encrypted: %w (%v)", ErrWrongPassword, err)
		}
		return nil, fmt.Errorf("archive: 7z: %w", err)
	}
	infos, err := r.ListFilesWithOffsets()
	if err != nil {
		return nil, fmt.Errorf("archive: 7z listing: %w", err)
	}
	// Where each folder's packed (encrypted) stream starts: the offset of
	// its first file, which sits at plaintext offset 0 of the folder.
	folderStart := map[int]int64{}
	for _, fi := range infos {
		if st, ok := folderStart[fi.FolderIndex]; !ok || fi.Offset < st {
			folderStart[fi.FolderIndex] = fi.Offset
		}
	}
	set := &Set{Volumes: ordered}
	for _, fi := range infos {
		if strings.HasSuffix(fi.Name, "/") {
			continue
		}
		m := Member{Name: cleanName(fi.Name), Size: int64(fi.Size)}
		switch {
		case fi.Compressed:
			m.Err = fmt.Errorf("%w: %s", ErrCompressed, m.Name)
		case fi.Encrypted && password == "":
			m.Err = fmt.Errorf("%w: %s: password needed", ErrEncrypted, m.Name)
		case fi.Encrypted:
			start := folderStart[fi.FolderIndex]
			skip := fi.Offset - start
			n := roundBlock(skip + int64(fi.Size))
			if len(fi.AESIV) != 16 || (fi.PackedSize > 0 && n > int64(fi.PackedSize)) || start < 0 || start+n > cat.size {
				m.Err = fmt.Errorf("%w: %s lies past the end of its encrypted folder", ErrIncomplete, m.Name)
				break
			}
			m.Encryption = &Encryption{Key: sevenZipKey(password, fi.AESSalt, fi.KDFIterations), IV: fi.AESIV, Skip: skip}
			m.Extents = cat.extents(start, n)
		case fi.Size > 0 && (fi.Offset < 0 || fi.Offset+int64(fi.Size) > cat.size):
			m.Err = fmt.Errorf("%w: %s lies past the end of the volumes", ErrIncomplete, m.Name)
		default:
			m.Extents = cat.extents(fi.Offset, int64(fi.Size))
		}
		set.Members = append(set.Members, m)
	}
	return set, nil
}

// headersEncrypted reports whether a 7z archive's header is itself
// encrypted (7z -mhe): its encoded header then names the AES coder.
func headersEncrypted(r io.ReaderAt) bool {
	var sh [32]byte
	if _, err := r.ReadAt(sh[:], 0); err != nil && err != io.EOF {
		return false
	}
	off := int64(32) + int64(binary.LittleEndian.Uint64(sh[12:20]))
	n := binary.LittleEndian.Uint64(sh[20:28])
	if n == 0 || n > 1<<20 {
		return false
	}
	h := make([]byte, n)
	if _, err := r.ReadAt(h, off); err != nil && err != io.EOF {
		return false
	}
	// kEncodedHeader (0x17) followed by streams info holding the AES coder
	// id 06 F1 07 01.
	return h[0] == 0x17 && bytes.Contains(h, []byte{0x06, 0xf1, 0x07, 0x01})
}

// concat presents volumes as one ReaderAt.
type concat struct {
	vols   []Volume
	starts []int64
	size   int64
}

func newConcat(vols []Volume) *concat {
	c := &concat{vols: vols}
	for _, v := range vols {
		c.starts = append(c.starts, c.size)
		c.size += v.Size
	}
	return c
}

// extents maps [off, off+n) of the joined stream onto volumes.
func (c *concat) extents(off, n int64) []Extent {
	var out []Extent
	for i, v := range c.vols {
		start, end := c.starts[i], c.starts[i]+v.Size
		if n <= 0 {
			break
		}
		if off >= end {
			continue
		}
		in := off - start
		take := min(n, v.Size-in)
		out = append(out, Extent{Volume: i, Offset: in, Length: take})
		off += take
		n -= take
	}
	return out
}

func (c *concat) ReadAt(p []byte, off int64) (int, error) {
	if off >= c.size {
		return 0, io.EOF
	}
	return (&extentReader{vols: c.vols, ext: c.extents(off, int64(len(p)))}).ReadAt(p, 0)
}
