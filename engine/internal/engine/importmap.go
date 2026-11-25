package engine

import (
	"fmt"
	"html"
	"strings"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// MappedSegment is one article of a mapped file: file bytes [Start, End]
// (inclusive, cli_mount's convention) come from decoded bytes starting at
// DataStart.
type MappedSegment struct {
	ID        string `json:"id"`
	Start     int64  `json:"start"`
	End       int64  `json:"end"`
	DataStart int64  `json:"data_start"`
}

// MappedFileSpec is one file of a migrated job.
type MappedFileSpec struct {
	Name     string          `json:"name"`
	Segments []MappedSegment `json:"segments"`
	// Encrypted members (cli_mount's derived AES key + IV): Segments map
	// the ciphertext and Size is the plaintext size.
	Key  []byte `json:"key,omitempty"`
	IV   []byte `json:"iv,omitempty"`
	Size int64  `json:"size,omitempty"`
}

// ImportMapped adds a job migrated from cli_mount: same id, folder name
// and file names, files read through cli_mount's own segment maps. It is
// ready at once (no usenet traffic); health checks work as for any job.
func (e *Engine) ImportMapped(id, name, category string, tags []string, files []MappedFileSpec) (store.Job, error) {
	if strings.TrimSpace(id) == "" || strings.TrimSpace(name) == "" || len(files) == 0 {
		return store.Job{}, fmt.Errorf("%w: id, name and files are required", ErrBadNZB)
	}
	res := &job.Result{}
	var doc strings.Builder
	doc.WriteString("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<nzb xmlns=\"http://www.newzbin.com/DTD/2003/nzb\">\n")
	doc.WriteString(" <head><meta type=\"title\">" + html.EscapeString(name) + "</meta><meta type=\"source\">cli_mount migration</meta></head>\n")
	seen := map[string]bool{}
	for i, f := range files {
		if f.Name == "" || seen[f.Name] || strings.ContainsAny(f.Name, "/\\") {
			return store.Job{}, fmt.Errorf("%w: file %d has a missing, duplicate or invalid name %q", ErrBadNZB, i, f.Name)
		}
		seen[f.Name] = true
		segs := make([]stream.MapSeg, len(f.Segments))
		fmt.Fprintf(&doc, " <file poster=\"cli_mount\" date=\"0\" subject=\"%s\">\n  <groups><group>alt.binaries.migrated</group></groups>\n  <segments>\n",
			html.EscapeString(`"`+f.Name+`"`))
		for k, sg := range f.Segments {
			if sg.ID == "" {
				return store.Job{}, fmt.Errorf("%w: %s segment %d has no message id", ErrBadNZB, f.Name, k)
			}
			segs[k] = stream.MapSeg{Start: sg.Start, Len: sg.End - sg.Start + 1, DataStart: sg.DataStart}
			fmt.Fprintf(&doc, "   <segment bytes=\"%d\" number=\"%d\">%s</segment>\n", segs[k].Len, k+1,
				html.EscapeString(strings.Trim(sg.ID, "<>")))
		}
		doc.WriteString("  </segments>\n </file>\n")
		size, err := stream.ValidateMap(segs)
		if err != nil {
			return store.Job{}, fmt.Errorf("%w: %s: %v", ErrBadNZB, f.Name, err)
		}
		src := job.Source{NZBIndex: i, Name: f.Name, Layout: stream.Layout{Size: size}, Map: segs,
			FirstSeg: strings.TrimSpace(strings.Trim(f.Segments[0].ID, "<>"))}
		if len(f.Key) > 0 || len(f.IV) > 0 {
			enc := stream.Encryption{Key: f.Key, IV: f.IV}
			if err := enc.Validate(); err != nil {
				return store.Job{}, fmt.Errorf("%w: %s: %v", ErrBadNZB, f.Name, err)
			}
			plain := f.Size
			if plain <= 0 {
				plain = size
			}
			if plain > size || size-plain >= 16 || size%16 != 0 {
				return store.Job{}, fmt.Errorf("%w: %s: encrypted map is %d bytes for a %d byte file", ErrBadNZB, f.Name, size, plain)
			}
			src.Encryption, size = &enc, plain
			src.Layout.Size = plain
		}
		res.Sources = append(res.Sources, src)
		res.Entries = append(res.Entries, job.Entry{Name: f.Name, Size: size,
			Pieces: []job.Piece{{Source: i, Offset: 0, Length: size}}})
	}
	doc.WriteString("</nzb>\n")
	j, err := e.Store.Create(store.Job{ID: id, Name: cleanFolderName(name), Category: category, Tags: tags},
		[]byte(doc.String()))
	if err != nil {
		return store.Job{}, err
	}
	if j, err = e.publish(id, res); err != nil {
		e.Store.Delete(id)
		return store.Job{}, err
	}
	return j, nil
}
