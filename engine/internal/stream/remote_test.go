package stream

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

type staticLink string

func (s staticLink) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	return string(s), nil
}

// Found live: chunks cached by an older chunk size (same key) crashed the
// engine with a slice panic. Keys now carry the size, and a chunk of the
// wrong length is an error, never a panic.
func TestRemoteChunkCacheSafety(t *testing.T) {
	file := bytes.Repeat([]byte("0123456789"), (RemoteChunk+5000)/10)
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var a, b int64
		fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &a, &b)
		w.WriteHeader(http.StatusPartialContent)
		w.Write(file[a : b+1])
	}))
	defer cdn.Close()
	cache := NewCache(64 << 20)
	cache.put("remote:F:0", []byte("old 1 MiB chunk")) // the old key format
	r := OpenRemote(staticLink(cdn.URL), nil, cache, "F", int64(len(file)), ReadAhead{})
	buf := make([]byte, 100)
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, 1<<20+50); err != nil || !bytes.Equal(buf, file[1<<20+50:1<<20+150]) {
		t.Fatalf("read with a stale old-format entry cached: %v", err)
	}
	cache.put(r.key(1), []byte("short")) // a wrong-length chunk under the current key
	if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, RemoteChunk+10); err == nil || !strings.Contains(err.Error(), "want") {
		t.Fatalf("a wrong-length chunk must be an error: %v", err)
	}
}

// A live read for a chunk a read-ahead is already downloading joins it:
// each chunk is fetched once.
func TestRemoteLiveJoinsReadAhead(t *testing.T) {
	file := bytes.Repeat([]byte("abcdefghij"), (6*RemoteChunk)/10)
	var mu sync.Mutex
	hits := map[string]int{}
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits[r.Header.Get("Range")]++
		mu.Unlock()
		time.Sleep(100 * time.Millisecond)
		var a, b int64
		fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &a, &b)
		w.WriteHeader(http.StatusPartialContent)
		w.Write(file[a : b+1])
	}))
	defer cdn.Close()
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "J", int64(len(file)), ReadAhead{Segments: 4})
	buf := make([]byte, 128<<10)
	for off := int64(0); off < int64(len(file)); off += int64(len(buf)) {
		if _, err := r.ReadAtCtx(context.Background(), nntp.Live, buf, off); err != nil && off+int64(len(buf)) < int64(len(file)) {
			t.Fatal(err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	for rng, n := range hits {
		if n != 1 {
			t.Fatalf("range %s fetched %d times: %v", rng, n, hits)
		}
	}
	if len(hits) != 6 {
		t.Fatalf("%d ranges fetched, want 6", len(hits))
	}
}
