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
}

// ErrLeftoverOnly means only leftovers (sample, extras) were streamable
// while the main content failed: importing would hand the library a sample
// instead of the real file.
var ErrLeftoverOnly = errors.New("job: only leftovers are streamable, main content failed")

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
		p.sf = stream.Open(f, cache, p.file, p.layout, 0)
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
	for i, name := range par2.Match(descs, cands) {
		candFiles[i].name = name
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
	for _, p := range plain {
		if strings.HasSuffix(strings.ToLower(p.name), ".par2") {
			continue
		}
		res.Entries = append(res.Entries, Entry{Name: p.name, Size: p.layout.Size,
			Pieces: []Piece{{Source: src(p), Offset: 0, Length: p.layout.Size}}})
	}

	var failedArchive int64 // largest failed archive content, for the leftover guard
	addSet := func(label string, vols []*posted, set *archive.Set, err error) {
		var total int64
		for _, v := range vols {
			total += v.layout.Size
		}
		if err != nil {
			res.Failures = append(res.Failures, Failure{Name: label, Size: total, Error: err.Error()})
			failedArchive = max(failedArchive, total)
			return
		}
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

	sort.SliceStable(res.Entries, func(i, j int) bool { return res.Entries[i].Size > res.Entries[j].Size })
	if len(res.Entries) == 0 {
		return res, ErrNothingStreamable
	}
	if LeftoverOnly(res.Entries[0].Size, failedArchive) {
		return res, fmt.Errorf("%w: largest file %s is %d bytes, failed archive content %d bytes",
			ErrLeftoverOnly, res.Entries[0].Name, res.Entries[0].Size, failedArchive)
	}
	return res, nil
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
