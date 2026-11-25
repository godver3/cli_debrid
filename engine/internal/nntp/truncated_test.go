package nntp

import (
	"bytes"
	"context"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/yenc"
)

// truncated is data's article with 40 lines lost from the middle (as a
// server that lost part of it serves it): its =ypart and =yend still
// claim the full size.
func truncated(id string, data []byte) nntptest.Article {
	body := nntptest.EncodePart("f.bin", int64(len(data)), 1, 1, 0, data)
	lines := bytes.SplitAfter(body, []byte("\r\n"))
	lines = append(lines[:20:20], lines[60:]...)
	return nntptest.Article{ID: id, Body: bytes.Join(lines, nil), Size: len(data)}
}

// cli_mount counts "a segment that decodes short" as dead. Here a server
// whose copy is shorter than its own =ypart says is skipped like a missing
// article: another server's full copy is used, and only short-or-missing
// everywhere is proof.
func TestTruncatedArticleIsMissingOnThatServer(t *testing.T) {
	for _, pure := range []bool{false, true} {
		t.Run(map[bool]string{false: "default", true: "pure-go"}[pure], func(t *testing.T) {
			old := yenc.UsePureGo
			yenc.UsePureGo = pure
			defer func() { yenc.UsePureGo = old }()
			truncatedCase(t)
		})
	}
}

func truncatedCase(t *testing.T) {
	data := awkward(50_000)
	full := nntptest.Post("t", "f.bin", data, len(data))[0]
	short := truncated(full.ID, data)

	a := nntptest.Start(t, short)
	b := nntptest.Start(t, full)
	c, err := NewClient([]Provider{provider(a, "a", 1), provider(b, "b", 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	got, _, err := c.Body(context.Background(), Live, "<"+full.ID+">")
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("full copy on the second server not used: %v", err)
	}

	only := nntptest.Start(t, short)
	c2, _ := NewClient([]Provider{provider(only, "a", 1)})
	defer c2.Close()
	if _, _, err := c2.Body(context.Background(), Live, "<"+full.ID+">"); !IsNotFound(err) {
		t.Fatalf("truncated everywhere must be proof, got %v", err)
	}
}
