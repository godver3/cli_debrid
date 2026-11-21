// Package job turns a parsed NZB into the list of files the mount serves:
// plain files as posted, and members of stored RAR/7z sets, with
// obfuscated names recovered from PAR2 or yEnc headers.
//
// The result is plain data (JSON-serialisable) so it can be stored and
// reopened without touching usenet again.
package job

import (
	"context"
	"errors"
	"fmt"
	"io"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/godver3/cli_debrid/engine/internal/archive"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/par2"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Source is one posted file the entries read from.
type Source struct {
	NZBIndex int           `json:"nzb_index"` // index into nzb.NZB.Files
	Name     string        `json:"name"`      // best known name
	Layout   stream.Layout `json:"layout"`
	// Map, when set, places the file's bytes in its articles directly
	// (jobs migrated from cli_mount); Layout then only carries the size.
	Map []stream.MapSeg `json:"map,omitempty"`
	// Remote, when set, is a debrid file reference: the file is read over
	// HTTP from a link cli_debrid provides (Layout carries the size).
	Remote string `json:"remote,omitempty"`
	// Encryption, when set on a mapped source, means Map holds AES-CBC
	// ciphertext (padded to whole blocks) and Layout.Size is the plaintext
	// size (encrypted RAR members migrated from cli_mount).
	Encryption *stream.Encryption `json:"encryption,omitempty"`
}

// MapIndex returns the 0-based segment of a mapped source holding off.
func (s Source) MapIndex(off int64) int {
	lo, hi := 0, len(s.Map)
	for lo < hi {
		m := (lo + hi) / 2
		if s.Map[m].Start+s.Map[m].Len > off {
			hi = m
		} else {
			lo = m + 1
		}
	}
	return lo
}

// Piece is a run of bytes of one source.
type Piece struct {
	Source int   `json:"source"` // index into Result.Sources
	Offset int64 `json:"offset"`
	Length int64 `json:"length"`
}

// Entry is a file the mount shows.
type Entry struct {
	Name   string  `json:"name"`
	Size   int64   `json:"size"`
	Pieces []Piece `json:"pieces"`
	// FromArchive names the archive set it came from ("" for plain files).
	FromArchive string `json:"from_archive,omitempty"`
}

// Failure is something that could not be made streamable.
type Failure struct {
	Name  string `json:"name"`
	Size  int64  `json:"size"`
	Error string `json:"error"`
}

// Result is a built job.
type Result struct {
	Sources  []Source  `json:"sources"`
	Entries  []Entry   `json:"entries"`
	Failures []Failure `json:"failures,omitempty"`
	// Notes say what Build did with the NZB's files (sets found, files
	// skipped and why), for logs and for "nothing streamable" errors.
	Notes []string `json:"notes,omitempty"`
	// MapsFile: the sources' segment maps were moved to the store's
	// maps.bin (this copy carries no Map); Store.Full puts them back.
	MapsFile bool `json:"maps_file,omitempty"`
}

// ErrLeftoverOnly means only leftovers (sample, extras) were streamable
// while the main content failed: importing would hand the library a sample
// instead of the real file.
var ErrLeftoverOnly = errors.New("job: only leftovers are streamable, main content failed")

// ErrInconclusive means the import failed for reasons that prove nothing
// (provider unreachable, timeouts): trying again later may work.
var ErrInconclusive = errors.New("job: import inconclusive")

// ErrBadMediaHeader: a media file rebuilt from an archive doesn't start
// with its container's header (archive volumes out of order).
var ErrBadMediaHeader = errors.New("job: media file does not start with a valid container header (archive volumes likely out of order)")

// ErrMissingContent means a posted file is proven missing (articles gone
// on every server) while another could not be checked: the release is dead
// either way, so the failure is final.
var ErrMissingContent = errors.New("job: content missing on every server")

// ErrNothingStreamable means no file could be served at all.
var ErrNothingStreamable = errors.New("job: nothing streamable")

// leftoverRatio: reject when the largest kept file is smaller than this
// share of the largest failed archive content (decypharr's rule, same
// cases in the tests).
const leftoverRatio = 0.25

// Options tune Build.
type Options struct {
	Parallel int // concurrent probes / volume parses (default 8)
}

var (
	reParVol  = regexp.MustCompile(`(?i)\.vol\d+[+-]\d+\.par2$`)
	reRarName = regexp.MustCompile(`(?i)(\.part\d+\.rar|\.rar|\.r\d{2,3})$`)
	re7zName  = regexp.MustCompile(`(?i)\.7z(\.\d{3})?$`)
	reSplit   = regexp.MustCompile(`(?i)\.\d{3}$`)
)

type posted struct {
	idx    int // nzb file index
	file   nzb.File
	name   string
	layout stream.Layout
	head   []byte // first bytes (up to 16 KiB)
	sf     *stream.File
	err    error
}

// Build probes every posted file and assembles the job.
func Build(ctx context.Context, f stream.Fetcher, cache *stream.Cache, n *nzb.NZB, opt Options) (*Result, error) {
	if opt.Parallel <= 0 {
		opt.Parallel = 8
	}
	files := make([]*posted, len(n.Files))
	for i, nf := range n.Files {
		files[i] = &posted{idx: i, file: nf, name: nf.Name}
	}

	// Probe everything except PAR2 recovery volumes (never needed).
	probe := func(p *posted) {
		p.layout, p.err = stream.Probe(ctx, f, cache, p.file)
		if p.err != nil {
			return
		}
		p.sf = stream.Open(f, cache, p.file, p.layout, stream.ReadAhead{})
		p.head = make([]byte, min(int64(par2.HashLen), p.layout.Size))
		if _, err := p.sf.ReadAtCtx(ctx, nntp.Live, p.head, 0); err != nil && err != io.EOF {
			p.err = err
		}
	}
	var todo []*posted
	for _, p := range files {
		if !reParVol.MatchString(p.name) {
			todo = append(todo, p)
		}
	}
	parallel(ctx, todo, opt.Parallel, probe)

	res := &Result{}
	note := func(format string, a ...any) { res.Notes = append(res.Notes, fmt.Sprintf(format, a...)) }
	note("%d files in the NZB, %d PAR2 recovery volumes skipped", len(files), len(files)-len(todo))
	// PAR2 index files (named *.par2 or recognised by magic) give real names.
	var descs []par2.FileDesc
	for _, p := range todo {
		if p.err != nil || !(strings.HasSuffix(strings.ToLower(p.name), ".par2") || strings.HasPrefix(string(p.head), "PAR2\x00PKT")) {
			continue
		}
		if p.layout.Size > 8<<20 {
			continue // an index file is small; this is a recovery volume
		}
		buf := make([]byte, p.layout.Size)
		if _, err := p.sf.ReadAtCtx(ctx, nntp.Live, buf, 0); err == nil || err == io.EOF {
			descs = append(descs, par2.Parse(buf)...)
		}
		p.name = "" // consumed; never shown
	}
	var cands []par2.Candidate
	var candFiles []*posted
	for _, p := range todo {
		if p.err == nil && p.name != "" {
			cands = append(cands, par2.Candidate{Head: p.head, Size: p.layout.Size})
			candFiles = append(candFiles, p)
		}
	}
	renamed := 0
	for i, name := range par2.Match(descs, cands) {
		candFiles[i].name = name
		renamed++
	}
	if len(descs) > 0 || renamed > 0 {
		note("PAR2 index lists %d files, %d posted files renamed from it", len(descs), renamed)
	}
	// Without PAR2, a yEnc header name beats an obfuscated subject.
	for _, p := range candFiles {
		if y := p.layout.YencName; y != "" && looksObfuscated(p.name) && !looksObfuscated(y) {
			p.name = y
		}
	}

	// Classify.
	var plain []*posted
	rarSets := map[string][]*posted{}
	var sevenZ []*posted
	for _, p := range todo {
		switch {
		case p.name == "":
			continue
		case p.err != nil:
			res.Failures = append(res.Failures, Failure{Name: p.name, Size: p.layout.Size, Error: p.err.Error()})
		case archive.DetectRAR(p.head) != 0:
			key := strings.ToLower(reRarName.ReplaceAllString(p.name, ""))
			if !reRarName.MatchString(p.name) {
				key = "\x00obfuscated" // one set for all RAR volumes without a usable name
			}
			rarSets[key] = append(rarSets[key], p)
		case archive.IsSevenZip(p.head) || (re7zName.MatchString(p.name) && reSplit.MatchString(p.name)):
			sevenZ = append(sevenZ, p)
		default:
			plain = append(plain, p)
		}
	}

	sourceOf := map[int]int{} // nzb index -> Result.Sources index
	src := func(p *posted) int {
		if i, ok := sourceOf[p.idx]; ok {
			return i
		}
		res.Sources = append(res.Sources, Source{NZBIndex: p.idx, Name: p.name, Layout: p.layout})
		sourceOf[p.idx] = len(res.Sources) - 1
		return len(res.Sources) - 1
	}
	skippedPar2 := 0
	for _, p := range plain {
		if strings.HasSuffix(strings.ToLower(p.name), ".par2") {
			skippedPar2++
			continue
		}
		res.Entries = append(res.Entries, Entry{Name: p.name, Size: p.layout.Size,
			Pieces: []Piece{{Source: src(p), Offset: 0, Length: p.layout.Size}}})
	}

	if skippedPar2 > 0 {
		note("%d other .par2 files skipped", skippedPar2)
	}
	if len(plain)-skippedPar2 > 0 {
		note("%d plain files", len(plain)-skippedPar2)
	}
	var failedArchive int64 // largest failed archive content, for the leftover guard
	addSet := func(label string, vols []*posted, set *archive.Set, err error) {
		var total int64
		for _, v := range vols {
			total += v.layout.Size
		}
		if err != nil {
			note("archive %s (%d volumes, %d bytes): %v", label, len(vols), total, err)
			res.Failures = append(res.Failures, Failure{Name: label, Size: total, Error: err.Error()})
			failedArchive = max(failedArchive, total)
			return
		}
		note("archive %s: %d volumes, %d files inside", label, len(set.Volumes), len(set.Members))
		byID := map[int]*posted{}
		for _, v := range vols {
			byID[v.idx] = v
		}
		for _, m := range set.Members {
			if m.Err != nil {
				res.Failures = append(res.Failures, Failure{Name: m.Name, Size: m.Size, Error: m.Err.Error()})
				failedArchive = max(failedArchive, m.Size)
				continue
			}
			e := Entry{Name: path.Base(m.Name), Size: m.Size, FromArchive: label}
			for _, x := range m.Extents {
				e.Pieces = append(e.Pieces, Piece{Source: src(byID[set.Volumes[x.Volume].ID]),
					Offset: x.Offset, Length: x.Length})
			}
			res.Entries = append(res.Entries, e)
		}
	}
	keys := make([]string, 0, len(rarSets))
	for k := range rarSets {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		vols := rarSets[k]
		set, err := archive.OpenRAR(ctx, volumesOf(vols), opt.Parallel)
		addSet(setLabel(vols), vols, set, err)
	}
	if len(sevenZ) > 0 {
		set, err := archive.OpenSevenZip(volumesOf(sevenZ))
		addSet(setLabel(sevenZ), sevenZ, set, err)
	}

	// A RAR set packed inside a 7z (Tulsa King S02E07 BROADCAST: a 7z of
	// .rar/.r00-.r45): open the inner set over the 7z members and map its
	// files back onto the posted articles, as cli_mount's 7z parser does.
	if len(res.Sources) > 0 {
		failedArchive = max(failedArchive, unnestRAR(ctx, res, todo, opt.Parallel, note))
	}

	// Media files rebuilt from archives must start with their container's
	// header: obfuscated volumes put in the wrong order still "parse" but
	// yield a file starting mid-stream that players can't open. A wrong
	// header is proof; a read error just skips the check.
	if len(res.Sources) > 0 {
		byIdx := map[int]*posted{}
		for _, p := range todo {
			byIdx[p.idx] = p
		}
		fs := &Files{}
		for _, s := range res.Sources {
			fs.sources = append(fs.sources, byIdx[s.NZBIndex].sf)
		}
		kept := res.Entries[:0]
		for _, e := range res.Entries {
			if e.FromArchive != "" && e.Size >= mediaHeaderLen {
				head := make([]byte, mediaHeaderLen)
				if _, err := fs.Entry(e).ReadAtCtx(ctx, nntp.Live, head, 0); err == nil && !mediaHeaderValid(e.Name, head) {
					res.Failures = append(res.Failures, Failure{Name: e.Name, Size: e.Size,
						Error: fmt.Sprintf("%v (starts %x)", ErrBadMediaHeader, head)})
					failedArchive = max(failedArchive, e.Size)
					continue
				}
			}
			kept = append(kept, e)
		}
		res.Entries = kept
	}
	sort.SliceStable(res.Entries, func(i, j int) bool { return res.Entries[i].Size > res.Entries[j].Size })
	// Proof (articles missing everywhere, bad archives) is final; a probe
	// that failed for network reasons only means "not yet". Even with other
	// files streamable, such a job is retried rather than made ready
	// without the file (an episode silently missing from a pack). But when
	// another posted file is already proven missing, the release is dead
	// whatever the unchecked file holds: fail now, like cli_mount's
	// fail-fast availability check, instead of retrying a dead NZB.
	var unsure, missing *posted
	for _, p := range todo {
		switch {
		case p.err == nil:
		case nntp.IsNotFound(p.err) || errors.Is(p.err, stream.ErrLayout):
			if missing == nil && isContent(p) {
				missing = p
			}
		case unsure == nil:
			unsure = p
		}
	}
	if unsure != nil {
		if missing == nil {
			return res, fmt.Errorf("%w: %s: %v", ErrInconclusive, unsure.name, unsure.err)
		}
		return res, fmt.Errorf("%w: %s: %v (%s could not be checked: %v)", ErrMissingContent, missing.name, missing.err, unsure.name, unsure.err)
	}
	if len(res.Entries) == 0 {
		why := res.Notes
		for _, f := range res.Failures {
			why = append(why, f.Name+": "+f.Error)
		}
		return res, fmt.Errorf("%w (%s)", ErrNothingStreamable, strings.Join(why, "; "))
	}
	if LeftoverOnly(res.Entries[0].Size, failedArchive) {
		return res, fmt.Errorf("%w: largest file %s is %d bytes, failed archive content %d bytes",
			ErrLeftoverOnly, res.Entries[0].Name, res.Entries[0].Size, failedArchive)
	}
	return res, nil
}

// reSideFile: posted files whose loss says nothing about the release
// (cli_mount skips par2/ignore files in its availability check too).
var reSideFile = regexp.MustCompile(`(?i)\.(par2|nfo|txt|sfv|srr|nzb|md5|url|jpe?g|png|srt|sub|idx|ass)$`)

// contentMinBytes: smaller posted files are treated as side files even
// with an obfuscated name.
const contentMinBytes = 5 << 20

// isContent reports whether p is real release content (media or an archive
// volume), so its articles missing everywhere prove the release dead.
func isContent(p *posted) bool {
	if reSideFile.MatchString(p.name) {
		return false
	}
	var n int64
	for _, sg := range p.file.Segments {
		n += int64(sg.Bytes)
	}
	return n >= contentMinBytes || p.layout.Size >= contentMinBytes
}

// unnestRAR replaces entries that are RAR volumes extracted from another
// archive with the files of that inner RAR set. Their pieces are the outer
// members' pieces sliced to each inner extent, so nothing is copied. It
// returns the size of an inner set that failed (for the leftover guard).
func unnestRAR(ctx context.Context, res *Result, todo []*posted, parallel int, note func(string, ...any)) int64 {
	sets := map[string][]int{} // outer archive + inner set key -> entry indexes
	for i, e := range res.Entries {
		if e.FromArchive == "" || !reRarName.MatchString(e.Name) {
			continue
		}
		key := e.FromArchive + "\x00" + strings.ToLower(reRarName.ReplaceAllString(e.Name, ""))
		sets[key] = append(sets[key], i)
	}
	if len(sets) == 0 {
		return 0
	}
	byIdx := map[int]*posted{}
	for _, p := range todo {
		byIdx[p.idx] = p
	}
	fs := &Files{}
	for _, s := range res.Sources {
		fs.sources = append(fs.sources, byIdx[s.NZBIndex].sf)
	}
	keys := make([]string, 0, len(sets))
	for k := range sets {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	drop := map[int]bool{}
	var added []Entry
	var failed int64
	for _, k := range keys {
		idxs := sets[k]
		vols := make([]archive.Volume, len(idxs))
		var total int64
		for i, ei := range idxs {
			e := res.Entries[ei]
			vols[i] = archive.Volume{Name: e.Name, R: fs.Entry(e), Size: e.Size, ID: ei}
			total += e.Size
		}
		label := res.Entries[idxs[0]].Name
		set, err := archive.OpenRAR(ctx, vols, parallel)
		if err != nil {
			note("inner archive %s (%d volumes inside %s): %v", label, len(vols), res.Entries[idxs[0]].FromArchive, err)
			res.Failures = append(res.Failures, Failure{Name: label, Size: total, Error: err.Error()})
			failed = max(failed, total)
			continue
		}
		note("inner archive %s: %d volumes, %d files inside", label, len(set.Volumes), len(set.Members))
		for _, ei := range idxs {
			drop[ei] = true
		}
		for _, m := range set.Members {
			if m.Err != nil {
				res.Failures = append(res.Failures, Failure{Name: m.Name, Size: m.Size, Error: m.Err.Error()})
				failed = max(failed, m.Size)
				continue
			}
			e := Entry{Name: path.Base(m.Name), Size: m.Size, FromArchive: label}
			for _, x := range m.Extents {
				outer := res.Entries[set.Volumes[x.Volume].ID]
				e.Pieces = append(e.Pieces, slicePieces(outer.Pieces, x.Offset, x.Length)...)
			}
			added = append(added, e)
		}
	}
	kept := res.Entries[:0]
	for i, e := range res.Entries {
		if !drop[i] {
			kept = append(kept, e)
		}
	}
	res.Entries = append(kept, added...)
	return failed
}

// slicePieces returns the pieces covering [off, off+n) of the bytes that
// pcs describe in order.
func slicePieces(pcs []Piece, off, n int64) []Piece {
	var out []Piece
	pos := int64(0)
	for _, pc := range pcs {
		end := pos + pc.Length
		if n > 0 && off < end {
			in := max(off-pos, 0)
			take := min(pc.Length-in, n)
			out = append(out, Piece{Source: pc.Source, Offset: pc.Offset + in, Length: take})
			off += take
			n -= take
		}
		pos = end
	}
	return out
}

// LeftoverOnly reports whether the largest streamable file is too small,
// next to the largest failed archive content, to be the real release.
func LeftoverOnly(largestKept, largestFailed int64) bool {
	return largestFailed > 0 && float64(largestKept) < leftoverRatio*float64(largestFailed)
}

func volumesOf(ps []*posted) []archive.Volume {
	vols := make([]archive.Volume, len(ps))
	for i, p := range ps {
		vols[i] = archive.Volume{Name: p.name, R: p.sf, Size: p.layout.Size, ID: p.idx}
	}
	return vols
}

func setLabel(ps []*posted) string {
	names := make([]string, len(ps))
	for i, p := range ps {
		names[i] = p.name
	}
	sort.Strings(names)
	return names[0]
}

var reObf = regexp.MustCompile(`^[A-Za-z0-9_-]{8,}(\.[A-Za-z0-9]{1,4})?$`)

// looksObfuscated: a long random token with no separators, like
// "PHMV2SbI78aIQ7wqt2.rar" or a bare hash.
func looksObfuscated(name string) bool {
	base := strings.TrimSuffix(name, path.Ext(name))
	if !reObf.MatchString(name) || strings.ContainsAny(base, ". ") {
		return false
	}
	digits, upper, lower := 0, 0, 0
	for _, r := range base {
		switch {
		case r >= '0' && r <= '9':
			digits++
		case r >= 'A' && r <= 'Z':
			upper++
		case r >= 'a' && r <= 'z':
			lower++
		}
	}
	return digits > 0 && (upper > 0 || lower > 0) || len(base) >= 24
}

func parallel(ctx context.Context, items []*posted, n int, fn func(*posted)) {
	sem := make(chan struct{}, n)
	var wg sync.WaitGroup
	for _, it := range items {
		wg.Add(1)
		go func(it *posted) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
				defer func() { <-sem }()
				fn(it)
			case <-ctx.Done():
				it.err = ctx.Err()
			}
		}(it)
	}
	wg.Wait()
}
