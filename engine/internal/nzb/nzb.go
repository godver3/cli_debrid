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

// NZB is a parsed NZB document.
type NZB struct {
	Meta  map[string]string // <head><meta type=...> (title, password, ...)
	Files []File
}

// Parse reads an NZB document.
func Parse(r io.Reader) (*NZB, error) {
	raw, err := parseRaw(r)
	if err != nil {
		return nil, err
	}
	if len(raw.Files) == 0 {
		return nil, fmt.Errorf("nzb: no files")
	}
	out := &NZB{Meta: raw.Meta}
	for _, f := range raw.Files {
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
	}
	if len(out.Files) == 0 {
		return nil, fmt.Errorf("nzb: no files with segments")
	}
	return out, nil
}

var yencCount = regexp.MustCompile(`(?i)yEnc\s*\((\d+)/(\d+)\)`)

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
func parseRaw(r io.Reader) (*nzbparser.Nzb, error) {
	x := new(rawNZB)
	dec := xml.NewDecoder(r)
	dec.CharsetReader = charset.NewReaderLabel
	dec.Strict = false // ignore unknown or malformed character entities
	if err := dec.Decode(x); err != nil {
		return nil, fmt.Errorf("unable to parse NZB file: %s", err.Error())
	}
	out := &nzbparser.Nzb{Meta: make(map[string]string)}
	for _, m := range x.Metadata {
		out.Meta[m.Type] = m.Value
	}
	// Merge <file> entries with the same subject, as nzbparser does.
	at := map[string]int{}
	for _, f := range x.Files {
		if i, ok := at[f.Subject]; ok {
			out.Files[i].Segments = append(out.Files[i].Segments, f.Segments...)
			continue
		}
		at[f.Subject] = len(out.Files)
		out.Files = append(out.Files, f)
	}
	nzbparser.ScanNzbFile(out)
	sort.Stable(out.Files)
	for i := range out.Files {
		sort.Stable(out.Files[i].Segments)
	}
	return out, nil
}
