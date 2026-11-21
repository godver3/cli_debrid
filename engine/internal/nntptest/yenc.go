// Package nntptest provides a fake NNTP server and a yEnc encoder so the
// engine can be tested end to end without the network.
package nntptest

import (
	"bytes"
	"fmt"
	"hash/crc32"
)

// EncodePart yEnc-encodes one part of a file as an NNTP article body,
// dot-stuffed and terminated with ".\r\n", exactly as a server sends it
// after "222".
//
// part is 1-based; offset is where data starts in the whole file.
func EncodePart(name string, fileSize int64, part, total int, offset int64, data []byte) []byte {
	const lineLen = 128
	var b bytes.Buffer
	fmt.Fprintf(&b, "=ybegin part=%d total=%d line=%d size=%d name=%s\r\n", part, total, lineLen, fileSize, name)
	fmt.Fprintf(&b, "=ypart begin=%d end=%d\r\n", offset+1, offset+int64(len(data)))

	line := make([]byte, 0, lineLen+4)
	flush := func() {
		// NNTP dot-stuffing: a line starting with '.' gets another '.'.
		if len(line) > 0 && line[0] == '.' {
			b.WriteByte('.')
		}
		b.Write(line)
		b.WriteString("\r\n")
		line = line[:0]
	}
	for _, c := range data {
		e := c + 42
		switch e {
		case 0, '\n', '\r', '=':
			line = append(line, '=', e+64)
		default:
			// Also escape a leading TAB/space/'.' like most posters do; the
			// '.' case still gets dot-stuffed above when it isn't escaped.
			if len(line) == 0 && (e == '\t' || e == ' ') {
				line = append(line, '=', e+64)
			} else {
				line = append(line, e)
			}
		}
		if len(line) >= lineLen {
			flush()
		}
	}
	if len(line) > 0 {
		flush()
	}
	fmt.Fprintf(&b, "=yend size=%d part=%d pcrc32=%08x\r\n", len(data), part, crc32.ChecksumIEEE(data))
	b.WriteString(".\r\n")
	return b.Bytes()
}

// Article is one posted segment.
type Article struct {
	ID   string // without angle brackets
	Body []byte // encoded body (EncodePart output)
	Size int    // decoded bytes, for NZB "bytes" attributes
}

// Post splits data into parts of partSize and encodes each one, naming the
// articles "<prefix>.<n>@test".
func Post(prefix, name string, data []byte, partSize int) []Article {
	total := (len(data) + partSize - 1) / partSize
	if total == 0 {
		total = 1
	}
	out := make([]Article, 0, total)
	for i := 0; i < total; i++ {
		start := i * partSize
		end := start + partSize
		if end > len(data) {
			end = len(data)
		}
		out = append(out, Article{
			ID:   fmt.Sprintf("%s.%d@test", prefix, i+1),
			Body: EncodePart(name, int64(len(data)), i+1, total, int64(start), data[start:end]),
			Size: end - start,
		})
	}
	return out
}
