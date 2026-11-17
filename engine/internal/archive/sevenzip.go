package archive

import (
	"bytes"
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
func OpenSevenZip(vols []Volume) (*Set, error) {
	if len(vols) == 0 {
		return nil, fmt.Errorf("archive: no volumes")
	}
	ordered := append([]Volume(nil), vols...)
	sort.SliceStable(ordered, func(a, b int) bool {
		return volumeNameKey(ordered[a].Name) < volumeNameKey(ordered[b].Name)
	})
	cat := newConcat(ordered)
	r, err := sevenzip.NewReaderWithPassword(cat, cat.size, "")
	if err != nil {
		return nil, fmt.Errorf("archive: 7z: %w", err)
	}
	infos, err := r.ListFilesWithOffsets()
	if err != nil {
		return nil, fmt.Errorf("archive: 7z listing: %w", err)
	}
	set := &Set{Volumes: ordered}
	for _, fi := range infos {
		if strings.HasSuffix(fi.Name, "/") {
			continue
		}
		m := Member{Name: cleanName(fi.Name), Size: int64(fi.Size)}
		switch {
		case fi.Encrypted:
			m.Err = fmt.Errorf("%w: %s", ErrEncrypted, m.Name)
		case fi.Compressed:
			m.Err = fmt.Errorf("%w: %s", ErrCompressed, m.Name)
		case fi.Size > 0 && (fi.Offset < 0 || fi.Offset+int64(fi.Size) > cat.size):
			m.Err = fmt.Errorf("%w: %s lies past the end of the volumes", ErrIncomplete, m.Name)
		default:
			m.Extents = cat.extents(fi.Offset, int64(fi.Size))
		}
		set.Members = append(set.Members, m)
	}
	return set, nil
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
