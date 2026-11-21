package stream

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
)

type idFetcher map[string][]byte

func (f idFetcher) Body(_ context.Context, _ nntp.Priority, id string) ([]byte, nntp.Meta, error) {
	if d, ok := f[id]; ok {
		return d, nntp.Meta{}, nil
	}
	return nil, nntp.Meta{}, nntp.ErrArticleNotFound
}

// A mapped file (as cli_mount stored it) whose NZB reuses a message-ID
// reads end to end: the reused position is not "segment not listed in NZB".
func TestMappedFileReadsReusedMessageID(t *testing.T) {
	doc := `<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file poster="p" date="1" subject="&quot;f.mkv&quot;">
<groups><group>g</group></groups><segments>
<segment bytes="4" number="1">a@x</segment><segment bytes="4" number="2">b@x</segment>
<segment bytes="4" number="3">a@x</segment></segments></file></nzb>`
	n, err := nzb.Parse(strings.NewReader(doc))
	if err != nil {
		t.Fatal(err)
	}
	// The same article serves two places, different slices of it.
	f := idFetcher{"a@x": []byte("AAAAaaaa"), "b@x": []byte("BBBB")}
	segs := []MapSeg{{Start: 0, Len: 4}, {Start: 4, Len: 4}, {Start: 8, Len: 4, DataStart: 4}}
	m := OpenMapped(f, NewCache(1<<20), n.Files[0], segs, ReadAhead{})
	got := make([]byte, 12)
	if _, err := m.ReadAtCtx(context.Background(), nntp.Live, got, 0); err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got, []byte("AAAABBBBaaaa")) {
		t.Fatalf("got %q", got)
	}
}
