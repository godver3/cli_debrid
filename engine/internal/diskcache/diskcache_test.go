package diskcache

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

func TestRoundTripEvictionAndReopen(t *testing.T) {
	dir := t.TempDir()
	c, err := Open(dir, 3*(100+headerLen))
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"a", "b", "c"} {
		c.Put(k, bytes.Repeat([]byte(k), 100))
	}
	c.Flush()
	if d, ok := c.Get("a"); !ok || !bytes.Equal(d, bytes.Repeat([]byte("a"), 100)) {
		t.Fatal("a missing")
	}
	c.Put("d", bytes.Repeat([]byte("d"), 100)) // evicts b (a was just used)
	c.Flush()
	if _, ok := c.Get("b"); ok {
		t.Fatal("b should have been evicted")
	}
	if n, used := c.Usage(); n != 3 || used != 3*(100+headerLen) {
		t.Fatalf("usage %d %d", n, used)
	}
	c2, err := Open(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"a", "c", "d"} {
		if _, ok := c2.Get(k); !ok {
			t.Fatalf("%s lost on reopen", k)
		}
	}
}

func TestTornFileIsDroppedNotServed(t *testing.T) {
	dir := t.TempDir()
	c, _ := Open(dir, 1<<20)
	c.Put("k", []byte("hello world"))
	c.Flush()
	p := c.path(hashName("k"))
	b, _ := os.ReadFile(p)
	os.WriteFile(p, b[:len(b)-3], 0o644) // crash mid-write
	if _, ok := c.Get("k"); ok {
		t.Fatal("torn file served")
	}
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatal("torn file left on disk")
	}
	// A leftover temp file is cleaned up at open.
	tmp := filepath.Join(dir, "ab", "x.tmp")
	os.MkdirAll(filepath.Dir(tmp), 0o755)
	os.WriteFile(tmp, []byte("x"), 0o644)
	Open(dir, 1<<20)
	if _, err := os.Stat(tmp); !os.IsNotExist(err) {
		t.Fatal("temp file left")
	}
}

func TestMemoryCacheUsesDiskBeforeNetwork(t *testing.T) {
	d, _ := Open(t.TempDir(), 1<<20)
	fetches := 0
	fetch := func(context.Context) ([]byte, error) { fetches++; return []byte("segment"), nil }

	first := stream.NewCache(1 << 20).WithDisk(d)
	if _, err := first.Do("seg@x", nntp.Live, fetch); err != nil {
		t.Fatal(err)
	}
	d.Flush()
	// A fresh memory cache (engine restart) finds it on disk.
	second := stream.NewCache(1 << 20).WithDisk(d)
	got, err := second.Do("seg@x", nntp.Live, fetch)
	if err != nil || string(got) != "segment" || fetches != 1 {
		t.Fatalf("got %q err %v fetches %d", got, err, fetches)
	}
}

func TestExpireDropsOnlyStaleSegments(t *testing.T) {
	c, _ := Open(t.TempDir(), 1<<20)
	c.Put("old", []byte("a"))
	c.Flush()
	time.Sleep(60 * time.Millisecond)
	c.Put("new", []byte("b"))
	c.Flush()
	if n := c.Expire(40 * time.Millisecond); n != 1 {
		t.Fatalf("expired %d, want 1", n)
	}
	if _, ok := c.Get("old"); ok {
		t.Fatal("stale segment still served")
	}
	if _, ok := c.Get("new"); !ok {
		t.Fatal("fresh segment expired")
	}
	if c.Expire(0) != 0 {
		t.Fatal("expiry 0 must mean off")
	}
}
