// Package nzb parses NZB files into the engine's own types.
package nzb

import (
	"encoding/xml"
	"fmt"
	"html"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/Tensai75/nzbparser"
	"golang.org/x/net/html/charset"
)

// Segment is one article of a file. Number is 1-based.
type Segment struct {
	ID     string
	Number int
	Bytes  int // encoded size from the NZB; only a size hint
}

// File is one posted file.
type File struct {
	Subject  string
	Name     string // filename from the subject, else the subject itself
	Poster   string
	Groups   []string
	Segments []Segment // sorted by Number, unique, gaps possible
	// Total is the highest segment number the NZB lists. Missing lists
	// numbers in 1..Total absent from Segments. The yEnc header of the first
	// article (stream.Probe) is what proves a file's real segment count.
	Total   int
	Missing []int
	// Announced is the "yEnc (1/N)" count from the subject, for reference
	// only: obfuscating posters fill it (and the [x/y] file counter) with
	// random numbers, so it can't decide anything.
	Announced int
}

// Complete reports whether every segment 1..Total is listed.
func (f File) Complete() bool { return len(f.Missing) == 0 }

// HasPrefix reports whether segments 1..n are all listed.
func (f File) HasPrefix(n int) bool {
	return n <= f.Total && (len(f.Missing) == 0 || f.Missing[0] > n)
}

// NZB is a parsed NZB document.
type NZB struct {
	Meta  map[string]string // <head><meta type=...> (title, password, ...)
	Files []File
	// stable lists Files indexes in the order engine 0.8.2 used (a stable
	// sort by subject number); see StableIndex.
	stable []int
}

// StableIndex returns the Files index of the file 0.8.2 put at position i,
// or -1. Jobs imported by 0.8.2 stored their sources in that order; for any
// other job it is unused (job.Result.SourceFiles decides).
func (n *NZB) StableIndex(i int) int {
	if i < 0 || i >= len(n.stable) {
		return -1
	}
	return n.stable[i]
}

// Parse reads an NZB document.
func Parse(r io.Reader) (*NZB, error) {
	raw, pos, err := parseRaw(r)
	if err != nil {
		return nil, err
	}
	var keptPos, keptNum []int // per out.Files: document position, subject number
	if len(raw.Files) == 0 {
		return nil, fmt.Errorf("nzb: no files")
	}
	out := &NZB{Meta: raw.Meta}
	for fi, f := range raw.Files {
		file := File{
			Subject: f.Subject,
			Name:    strings.TrimSpace(f.Filename),
			Poster:  f.Poster,
			Groups:  f.Groups,
		}
		if n, ok := announcedSegments(f.Subject); ok {
			file.Announced = n
		}
		if file.Name == "" {
			file.Name = strings.TrimSpace(f.Subject)
		}
		have := make(map[int]bool, len(f.Segments))
		for _, s := range f.Segments {
			// nzbparser keeps the raw inner XML, so entities (&amp;, &lt;...)
			// are still escaped; the server needs the literal message-id.
			id := strings.TrimSpace(html.UnescapeString(s.Id))
			id = strings.TrimSuffix(strings.TrimPrefix(id, "<"), ">")
			if id == "" || s.Number <= 0 || have[s.Number] {
				continue
			}
			have[s.Number] = true
			file.Segments = append(file.Segments, Segment{ID: id, Number: s.Number, Bytes: s.Bytes})
		}
		if len(file.Segments) == 0 {
			continue
		}
		file.Total = file.Segments[len(file.Segments)-1].Number
		for n := 1; n <= file.Total; n++ {
			if !have[n] {
				file.Missing = append(file.Missing, n)
			}
		}
		out.Files = append(out.Files, file)
		keptPos = append(keptPos, pos[fi])
		keptNum = append(keptNum, f.Number)
	}
	if len(out.Files) == 0 {
		return nil, fmt.Errorf("nzb: no files with segments")
	}
	out.stable = make([]int, len(out.Files))
	for i := range out.stable {
		out.stable[i] = i
	}
	sort.SliceStable(out.stable, func(a, b int) bool {
		x, y := out.stable[a], out.stable[b]
		if keptNum[x] != keptNum[y] {
			return keptNum[x] < keptNum[y]
		}
		return keptPos[x] < keptPos[y]
	})
	return out, nil
}

var yencCount = regexp.MustCompile(`(?i)yEnc\s*\((\d+)/(\d+)\)`)

// posterLooksLikeSubject spots a file description ("x.part01.rar" yEnc
// (1/50)) in a poster attribute, as opposed to an uploader name.
var posterLooksLikeSubject = regexp.MustCompile(`(?i)yenc|"|\.par2|\.rar|\.r\d\d|\.7z|\.mkv|\.mp4|\.avi`)

// announcedSegments reads N from a subject's "yEnc (1/N)".
func announcedSegments(subject string) (int, bool) {
	m := yencCount.FindStringSubmatch(subject)
	if m == nil {
		return 0, false
	}
	n, err := strconv.Atoi(m[2])
	return n, err == nil && n > 0
}

// rawNZB mirrors nzbparser's decode structure.
type rawNZB struct {
	XMLName  xml.Name           `xml:"nzb"`
	Metadata []rawMeta          `xml:"head>meta"`
	Files    nzbparser.NzbFiles `xml:"file"`
}

type rawMeta struct {
	Type  string `xml:"type,attr"`
	Value string `xml:",innerxml"`
}

// parseRaw is nzbparser.Parse without its drop-by-message-ID step. Some
// posters reuse one message-ID at more than one segment number (Blue Lights
// S01E02 had 53, an Austin City Limits episode every ID twice); nzbparser
// kept only the first, the dropped numbers had no article, and reads there
// were taken as "missing on every provider" and the file marked broken.
// cli_mount keeps every number. Repeated numbers are still dropped in Parse.
func parseRaw(r io.Reader) (*nzbparser.Nzb, []int, error) {
	x := new(rawNZB)
	dec := xml.NewDecoder(r)
	dec.CharsetReader = charset.NewReaderLabel
	dec.Strict = false // ignore unknown or malformed character entities
	if err := dec.Decode(x); err != nil {
		return nil, nil, fmt.Errorf("unable to parse NZB file: %s", err.Error())
	}
	out := &nzbparser.Nzb{Meta: make(map[string]string)}
	for _, m := range x.Metadata {
		out.Meta[m.Type] = m.Value
	}
	// Merge <file> entries with the same subject, as nzbparser does. Some
	// NZB writers leave subject empty and put the file description in
	// poster: take it from there, and never merge files that still have no
	// subject (merging glued every file into one unreadable blob).
	at := map[string]int{}
	for _, f := range x.Files {
		if strings.TrimSpace(f.Subject) == "" {
			if posterLooksLikeSubject.MatchString(f.Poster) {
				f.Subject = f.Poster
			} else {
				out.Files = append(out.Files, f)
				continue
			}
		}
		if i, ok := at[f.Subject]; ok {
			out.Files[i].Segments = append(out.Files[i].Segments, f.Segments...)
			continue
		}
		at[f.Subject] = len(out.Files)
		out.Files = append(out.Files, f)
	}
	nzbparser.ScanNzbFile(out)
	// Exactly nzbparser's sorts, not sort.Stable: a job stores its sources
	// as positions in this file list (Source.NZBIndex), so the order must
	// match what every existing job was imported with. Files often share a
	// subject number (obfuscated posts), and a stable sort orders those
	// differently: 0.8.2 shipped that and pre-0.8.2 jobs read the wrong NZB
	// files (Dark Matter S02E10 d3g, Dutton Ranch S01E07 marked broken).
	// The wrapper swaps a position list along with the files; pdqsort's
	// moves depend only on Less, so the file order is nzbparser's exactly.
	w := filesWithPos{out.Files, make([]int, len(out.Files))}
	for i := range w.pos {
		w.pos[i] = i
	}
	sort.Sort(w)
	for i := range out.Files {
		sort.Sort(out.Files[i].Segments)
	}
	return out, w.pos, nil
}

type filesWithPos struct {
	f   nzbparser.NzbFiles
	pos []int
}

func (w filesWithPos) Len() int           { return len(w.f) }
func (w filesWithPos) Less(i, j int) bool { return w.f.Less(i, j) }
func (w filesWithPos) Swap(i, j int) {
	w.f.Swap(i, j)
	w.pos[i], w.pos[j] = w.pos[j], w.pos[i]
}
