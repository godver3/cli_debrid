package stream

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/iotest"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Found live: with nothing playing, stopped debrid streams of a played pack
// still held 684 MB of chunk buffers, and appended chunks were cached in
// arrays up to a quarter larger than their bytes; the heap sat over the GC
// limit and the GC ran ~47 times a second.

func readAll(t *testing.T, r *RemoteFile, size int64) []byte {
	t.Helper()
	got := make([]byte, 0, size)
	buf := make([]byte, 128<<10)
	for off := int64(0); off < size; {
		n, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, off)
		got = append(got, buf[:n]...)
		off += int64(n)
		if err != nil && err != io.EOF {
			t.Fatal(err)
		}
		if n == 0 {
			break
		}
	}
	return got
}

func TestStoppedStreamReleasesBuffers(t *testing.T) {
	file := bytes.Repeat([]byte("abcdefghij"), (2*RemoteChunk+54321)/10)
	cdn := httptest.NewServer(serveRanges(file, 0, nil))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "M", int64(len(file)), ReadAhead{Bytes: 8 << 20})
	if got := readAll(t, r, int64(len(file))); !bytes.Equal(got, file) {
		t.Fatal("bytes differ")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		r.smu.Lock()
		held, live := 0, 0
		for _, d := range r.streams {
			if !d.done {
				live++
			}
			held += cap(d.buf) + cap(d.prevBuf)
		}
		r.smu.Unlock()
		if live == 0 {
			if held != 0 {
				t.Fatalf("stopped streams still hold %d buffer bytes", held)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("stream did not stop at the end of the file")
		}
		time.Sleep(20 * time.Millisecond)
	}
	// A read after the stream stopped still works (cache, or a new stream).
	buf := make([]byte, 1000)
	off := int64(RemoteChunk + 777)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, off); err != nil || !bytes.Equal(buf, file[off:off+1000]) {
		t.Fatalf("read after stop: %v", err)
	}
}

func TestStreamedChunksAreCachedAtExactSize(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (2*RemoteChunk+10)/10)
	cdn := httptest.NewServer(serveRanges(file, 0, nil))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "X", int64(len(file)), ReadAhead{})
	readAll(t, r, int64(len(file)))
	for i := 0; i < 2; i++ {
		d, ok := r.cache.Get(r.key(i))
		if !ok {
			t.Fatalf("chunk %d not cached", i)
		}
		if cap(d) != len(d) {
			t.Fatalf("chunk %d cached with cap %d for %d bytes", i, cap(d), len(d))
		}
	}
}

func TestCacheBudgetCountsCapacity(t *testing.T) {
	c := NewCache(1000)
	c.Put("a", make([]byte, 100, 600))
	c.Put("b", make([]byte, 100, 600)) // 1200 of capacity: a goes
	if _, ok := c.Get("a"); ok {
		t.Fatal("a should be evicted: the budget is capacity, not length")
	}
	if n, used := c.Len(); n != 1 || used != 600 {
		t.Fatalf("len=%d used=%d, want 1 and 600", n, used)
	}
	c.Put("huge", make([]byte, 10, 2000)) // over the whole budget: not cached
	if _, ok := c.Get("huge"); ok {
		t.Fatal("an array larger than the budget must not be cached")
	}
}

func TestReadExact(t *testing.T) {
	for _, tc := range []struct {
		body    string
		n       int64
		wantLen int
	}{
		{"hello", 5, 5},
		{"hel", 5, 3},    // short
		{"hello!", 5, 6}, // longer than asked: caller sees len != n
		{"", 3, 0},
	} {
		got, err := readExact(iotest.OneByteReader(strings.NewReader(tc.body)), tc.n)
		if err != nil || len(got) != tc.wantLen {
			t.Fatalf("%q n=%d: len %d err %v, want len %d", tc.body, tc.n, len(got), err, tc.wantLen)
		}
		if tc.wantLen == int(tc.n) && (cap(got) != int(tc.n) || string(got) != tc.body) {
			t.Fatalf("%q: got %q cap %d", tc.body, got, cap(got))
		}
	}
	boom := errors.New("boom")
	if _, err := readExact(iotest.ErrReader(boom), 4); !errors.Is(err, boom) {
		t.Fatalf("read error must surface: %v", err)
	}
}
