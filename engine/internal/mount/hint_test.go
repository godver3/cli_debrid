package mount

import (
	"context"
	"testing"
)

func TestHandleReadAheadGrowsWithSequentialReads(t *testing.T) {
	h := &openFile{next: -1, ctx: context.Background()}
	const r = 128 << 10
	if got := h.hint(0, r).Ahead; got != minAhead {
		t.Fatalf("first read: ahead %d, want %d", got, minAhead)
	}
	var off int64 = r
	for ; off < 8<<20; off += r {
		h.hint(off, r)
	}
	if got := h.hint(off, r).Ahead; got < 16<<20 {
		t.Fatalf("after 8 MB sequential: ahead %d, want >= 16 MB", got)
	}
	// Kernel readahead reorders reads: slightly behind still counts.
	if got := h.hint(off-r, r).Ahead; got < 16<<20 {
		t.Fatalf("reordered read reset the window: %d", got)
	}
	// A seek (a probe jumping to the tail) starts small again.
	if got := h.hint(4<<30, r).Ahead; got != minAhead {
		t.Fatalf("after a seek: ahead %d, want %d", got, minAhead)
	}
}
