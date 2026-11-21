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

// A handle that keeps jumping (Plex thumbnails, intro/credit detection)
// loses the minimum read-ahead: found live, a thumbnail pass reading a few
// hundred KB per jump pulled ~10 MB/s through the full read-ahead. Once it
// reads in order for a while (playback after its seeks), it gets it back.
func TestScannerHandleLosesMinimumReadAhead(t *testing.T) {
	h := &openFile{next: -1, ctx: context.Background()}
	const r = 256 << 10
	// Playback-like start: head, tail, resume point: still the minimum.
	for _, off := range []int64{0, 9 << 30, 2 << 30} {
		if got := h.hint(off, r).Ahead; got != minAhead {
			t.Fatalf("jump to %d: ahead %d, want %d", off, got, minAhead)
		}
	}
	// A scanner keeps jumping: only what its own reading earns.
	var got int64
	for i := int64(1); i <= 6; i++ {
		got = h.hint(i*(1<<30)+5<<20, r).Ahead
	}
	if got != 2*r {
		t.Fatalf("scanner: ahead %d, want %d", got, 2*r)
	}
	// Reading in order again restores normal read-ahead.
	off := int64(7 << 30)
	for end := off + scanSeq; off <= end; off += r {
		h.hint(off, r)
	}
	if got := h.hint(9<<29, r).Ahead; got != minAhead {
		t.Fatalf("after reading %d MB in order, a seek: ahead %d, want %d", scanSeq>>20, got, minAhead)
	}
}
