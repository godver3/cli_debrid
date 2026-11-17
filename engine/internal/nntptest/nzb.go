package nntptest

import (
	"fmt"
	"html"
	"strings"
)

// File is one file to post in an NZB.
type File struct {
	Name string
	Data []byte
}

// PostNZB posts files to s (articles "<prefix><n>.<part>@test", 64 kB
// parts) and returns an NZB document listing them.
func PostNZB(s *Server, prefix string, files ...File) []byte {
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="utf-8"?>` + "\n" + `<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">` + "\n")
	for i, f := range files {
		arts := Post(fmt.Sprintf("%s%d", prefix, i), f.Name, f.Data, 64_000)
		s.Add(arts...)
		subj := fmt.Sprintf(`[%d/%d] - "%s" yEnc (1/%d)`, i+1, len(files), f.Name, len(arts))
		fmt.Fprintf(&b, " <file poster=\"p\" date=\"1700000000\" subject=\"%s\">\n  <groups><group>alt.binaries.test</group></groups>\n  <segments>\n", html.EscapeString(subj))
		for n, a := range arts {
			fmt.Fprintf(&b, "   <segment bytes=\"%d\" number=\"%d\">%s</segment>\n", len(a.Body), n+1, a.ID)
		}
		b.WriteString("  </segments>\n </file>\n")
	}
	b.WriteString("</nzb>\n")
	return []byte(b.String())
}
