package archive

import (
	"context"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// Volume is one posted archive volume.
type Volume struct {
	Name string
	R    io.ReaderAt
	Size int64
	ID   int // caller's own reference, carried through unchanged
}

// Extent is a contiguous run of member data inside one volume.
type Extent struct {
	Volume int // index into the ordered volume list
	Offset int64
	Length int64
}

// Member is a streamable file inside an archive set.
type Member struct {
	Name    string
	Size    int64
	Extents []Extent
	// Encryption is set for a password-protected member: Extents then hold
	// AES-CBC ciphertext (padded to whole blocks), Size is the plaintext size.
	Encryption *Encryption
	// Err is set when the member cannot be streamed (compressed, encrypted,
	// incomplete). Size and Name are still filled in for reporting.
	Err error
}

// Set is a parsed archive set with its volumes in archive order.
type Set struct {
	Volumes []Volume
	Members []Member
	// OrderedByHeaders is true when volume order came from the archive's
	// own volume numbers (immune to obfuscated names).
	OrderedByHeaders bool
}

// ReaderAt returns a reader over a member's (plaintext) data.
func (s *Set) ReaderAt(m Member) io.ReaderAt {
	r := &extentReader{vols: s.Volumes, ext: m.Extents}
	if m.Encryption == nil {
		return r
	}
	return &decryptReader{src: r, enc: *m.Encryption, size: m.Size, cipher: extentsLen(m.Extents)}
}

// OpenRAR parses every volume (in parallel, at most `parallel` at once) and
// stitches members across them.
func OpenRAR(ctx context.Context, vols []Volume, parallel int) (*Set, error) {
	return OpenRARPassword(ctx, vols, parallel, "")
}

// OpenRARPassword is OpenRAR for sets that may be password protected.
func OpenRARPassword(ctx context.Context, vols []Volume, parallel int, password string) (*Set, error) {
	if len(vols) == 0 {
		return nil, fmt.Errorf("archive: no volumes")
	}
	if parallel < 1 {
		parallel = 1
	}
	infos := make([]*VolumeInfo, len(vols))
	errs := make([]error, len(vols))
	sem := make(chan struct{}, parallel)
	var wg sync.WaitGroup
	for i := range vols {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				errs[i] = ctx.Err()
				return
			}
			defer func() { <-sem }()
			infos[i], errs[i] = ParseVolumePassword(vols[i].R, vols[i].Size, password)
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			return nil, fmt.Errorf("archive: volume %s: %w", vols[i].Name, err)
		}
	}

	order, byHeaders := volumeOrder(vols, infos)
	set := &Set{OrderedByHeaders: byHeaders}
	ordered := make([]*VolumeInfo, len(order))
	for i, idx := range order {
		set.Volumes = append(set.Volumes, vols[idx])
		ordered[i] = infos[idx]
	}
	set.Members = stitch(ordered, password)
	return set, nil
}

// volumeOrder sorts by header volume numbers when every volume has a
// distinct one, else by name.
func volumeOrder(vols []Volume, infos []*VolumeInfo) ([]int, bool) {
	order := make([]int, len(vols))
	for i := range order {
		order[i] = i
	}
	seen := map[int]bool{}
	byHeaders := len(vols) > 0
	for _, in := range infos {
		if in.Number < 0 || seen[in.Number] {
			byHeaders = false
			break
		}
		seen[in.Number] = true
	}
	if byHeaders {
		sort.SliceStable(order, func(a, b int) bool { return infos[order[a]].Number < infos[order[b]].Number })
		return order, true
	}
	sort.SliceStable(order, func(a, b int) bool {
		return volumeNameKey(vols[order[a]].Name) < volumeNameKey(vols[order[b]].Name)
	})
	return order, false
}

var (
	rePart = regexp.MustCompile(`(?i)\.part0*(\d+)\.rar$`)
	reRNN  = regexp.MustCompile(`(?i)\.r(\d{2,3})$`)
	reNum  = regexp.MustCompile(`(?i)\.(\d{3})$`)
)

// volumeNameKey sorts "x.part01.rar" < "x.part02.rar", and "x.rar" <
// "x.r00" < "x.r01" for old-style sets. Unknown names sort by name.
func volumeNameKey(name string) string {
	switch {
	case rePart.MatchString(name):
		n, _ := strconv.Atoi(rePart.FindStringSubmatch(name)[1])
		return fmt.Sprintf("%s|%08d", strings.ToLower(rePart.ReplaceAllString(name, "")), n)
	case strings.HasSuffix(strings.ToLower(name), ".rar"):
		return fmt.Sprintf("%s|%08d", strings.ToLower(name[:len(name)-4]), 0)
	case reRNN.MatchString(name):
		n, _ := strconv.Atoi(reRNN.FindStringSubmatch(name)[1])
		return fmt.Sprintf("%s|%08d", strings.ToLower(reRNN.ReplaceAllString(name, "")), n+1)
	case reNum.MatchString(name):
		n, _ := strconv.Atoi(reNum.FindStringSubmatch(name)[1])
		return fmt.Sprintf("%s|%08d", strings.ToLower(reNum.ReplaceAllString(name, "")), n)
	}
	return strings.ToLower(name) + "|"
}

// stitch joins each member's parts across ordered volumes. An encrypted
// RAR5 member is one AES-CBC stream across its parts, keyed from its first
// part's record (as unrar and 7-Zip read it).
func stitch(infos []*VolumeInfo, password string) []Member {
	var members []Member
	open := map[string]int{} // name -> index in members, while SplitAfter
	for vi, in := range infos {
		for _, p := range in.Parts {
			if p.Dir {
				continue
			}
			idx, cont := open[p.Name]
			if p.SplitBefore && !cont {
				// Continuation of a member whose start we never saw.
				members = append(members, Member{Name: p.Name, Size: p.Unpacked,
					Err: fmt.Errorf("%w: %s starts in a missing volume", ErrIncomplete, p.Name)})
				continue
			}
			if !p.SplitBefore {
				members = append(members, Member{Name: p.Name, Size: p.Unpacked})
				idx = len(members) - 1
			}
			m := &members[idx]
			switch {
			case m.Err != nil:
			case p.Encrypted && p.Crypt == nil:
				m.Err = fmt.Errorf("%w: %s (RAR4 encryption is not supported)", ErrEncrypted, p.Name)
			case !p.Stored:
				m.Err = fmt.Errorf("%w: %s", ErrCompressed, p.Name)
			case p.Encrypted && !p.SplitBefore:
				key, err := p.Crypt.open(password)
				if err != nil {
					m.Err = fmt.Errorf("%w: %s", err, p.Name)
					break
				}
				m.Encryption = &Encryption{Key: key, IV: p.Crypt.IV}
				m.Extents = append(m.Extents, Extent{Volume: vi, Offset: p.Offset, Length: p.Length})
			case p.Encrypted != (m.Encryption != nil):
				m.Err = fmt.Errorf("%w: %s is encrypted in some volumes only", ErrCorrupt, p.Name)
			default:
				m.Extents = append(m.Extents, Extent{Volume: vi, Offset: p.Offset, Length: p.Length})
			}
			if p.SplitAfter {
				open[p.Name] = idx
			} else {
				delete(open, p.Name)
			}
		}
	}
	for i := range members {
		m := &members[i]
		if m.Err != nil {
			continue
		}
		got := extentsLen(m.Extents)
		switch {
		case m.Encryption != nil:
			// Ciphertext is the plaintext padded to whole AES blocks.
			if got != roundBlock(m.Size) {
				m.Err = fmt.Errorf("%w: %s has %d of %d encrypted bytes", ErrIncomplete, m.Name, got, roundBlock(m.Size))
			}
		case got != m.Size:
			m.Err = fmt.Errorf("%w: %s has %d of %d bytes", ErrIncomplete, m.Name, got, m.Size)
		}
	}
	return members
}

// extentReader serves a member's bytes from its extents.
type extentReader struct {
	vols []Volume
	ext  []Extent
}

func (r *extentReader) ReadAt(p []byte, off int64) (int, error) {
	n := 0
	pos := int64(0) // member offset where the current extent starts
	for _, e := range r.ext {
		if n == len(p) {
			break
		}
		end := pos + e.Length
		if off+int64(n) < end {
			in := off + int64(n) - pos // offset inside this extent
			want := min(int64(len(p)-n), e.Length-in)
			got, err := r.vols[e.Volume].R.ReadAt(p[n:n+int(want)], e.Offset+in)
			n += got
			if err != nil && !(err == io.EOF && int64(got) == want) {
				return n, err
			}
		}
		pos = end
	}
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func extentsLen(ext []Extent) int64 {
	var n int64
	for _, e := range ext {
		n += e.Length
	}
	return n
}

// decryptReader serves the plaintext of an encrypted member (archive
// parsing and tests; the mount decrypts in package stream).
type decryptReader struct {
	src    io.ReaderAt
	enc    Encryption
	size   int64 // plaintext member size
	cipher int64 // ciphertext bytes in src
}

func (d *decryptReader) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("archive: negative offset")
	}
	if off >= d.size {
		return 0, io.EOF
	}
	end := min(off+int64(len(p)), d.size)
	const bs = 16
	lo, hi := off+d.enc.Skip, end+d.enc.Skip // in the decrypted stream
	aStart := lo - lo%bs
	aEnd := min(roundBlock(hi), d.cipher)
	from := max(aStart-bs, 0)
	buf := make([]byte, aEnd-from)
	if n, err := d.src.ReadAt(buf, from); n < len(buf) {
		if err == nil || err == io.EOF {
			err = fmt.Errorf("%w: encrypted data ends at %d, need %d", ErrIncomplete, from+int64(n), aEnd)
		}
		return 0, err
	}
	iv, ct := d.enc.IV, buf
	if aStart > 0 {
		iv, ct = buf[:bs], buf[bs:]
	}
	if err := decryptCBC(d.enc.Key, iv, ct); err != nil {
		return 0, err
	}
	n := copy(p, ct[lo-aStart:hi-aStart])
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}
