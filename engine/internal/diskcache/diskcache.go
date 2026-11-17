// Package diskcache keeps decoded segments on disk, bounded by bytes with
// least-recently-used eviction. Each file carries its length and CRC32, so
// a torn write (crash mid-write) or bit rot is detected and dropped rather
// than served.
//
//	<dir>/<ab>/<sha1 of key>   ("ab" = first two hex chars)
package diskcache

import (
	"container/list"
	"crypto/sha1"
	"encoding/binary"
	"encoding/hex"
	"hash/crc32"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const headerLen = 8 // uint32 length + uint32 CRC32

// Cache is safe for concurrent use.
type Cache struct {
	dir   string
	max   int64
	mu    sync.Mutex
	used  int64
	ll    *list.List // front = most recently used
	items map[string]*list.Element

	writes  chan write
	pending sync.WaitGroup

	Hits, Misses, Dropped atomic.Int64
}

type item struct {
	name string    // hashed file name
	size int64     // bytes on disk
	at   time.Time // last written or read
}

type write struct {
	name string
	data []byte
}

// Open indexes an existing cache dir (created if missing). Files beyond
// maxBytes are evicted oldest first.
func Open(dir string, maxBytes int64) (*Cache, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	c := &Cache{dir: dir, max: maxBytes, ll: list.New(), items: map[string]*list.Element{},
		writes: make(chan write, 64)}
	type found struct {
		name string
		size int64
		mod  int64
	}
	var all []found
	_ = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		name := d.Name()
		if len(name) != 40 || filepath.Base(filepath.Dir(p)) != name[:2] {
			if filepath.Ext(name) == ".tmp" {
				os.Remove(p) // leftover from an interrupted write
			}
			return nil
		}
		if info, err := d.Info(); err == nil {
			all = append(all, found{name, info.Size(), info.ModTime().UnixNano()})
		}
		return nil
	})
	sort.Slice(all, func(i, j int) bool { return all[i].mod < all[j].mod }) // oldest first
	for _, f := range all {
		c.items[f.name] = c.ll.PushFront(&item{name: f.name, size: f.size, at: time.Unix(0, f.mod)})
		c.used += f.size
	}
	c.mu.Lock()
	c.evictLocked()
	c.mu.Unlock()
	go c.writer()
	return c, nil
}

func hashName(key string) string {
	h := sha1.Sum([]byte(key))
	return hex.EncodeToString(h[:])
}

func (c *Cache) path(name string) string { return filepath.Join(c.dir, name[:2], name) }

// Get returns a cached segment.
func (c *Cache) Get(key string) ([]byte, bool) {
	name := hashName(key)
	c.mu.Lock()
	el, ok := c.items[name]
	if ok {
		c.ll.MoveToFront(el)
		el.Value.(*item).at = time.Now()
	}
	c.mu.Unlock()
	if !ok {
		c.Misses.Add(1)
		return nil, false
	}
	b, err := os.ReadFile(c.path(name))
	if err != nil || len(b) < headerLen {
		c.drop(name)
		c.Misses.Add(1)
		return nil, false
	}
	n := binary.LittleEndian.Uint32(b[0:4])
	sum := binary.LittleEndian.Uint32(b[4:8])
	data := b[headerLen:]
	if int(n) != len(data) || crc32.ChecksumIEEE(data) != sum {
		c.drop(name) // torn or corrupt: never serve it
		c.Misses.Add(1)
		return nil, false
	}
	c.Hits.Add(1)
	return data, true
}

// Put stores a segment in the background. If the writer is backed up the
// segment is skipped: the disk tier is an optimisation, never a stall.
func (c *Cache) Put(key string, data []byte) {
	if int64(len(data)+headerLen) > c.max {
		return
	}
	name := hashName(key)
	c.mu.Lock()
	_, have := c.items[name]
	c.mu.Unlock()
	if have {
		return
	}
	c.pending.Add(1)
	select {
	case c.writes <- write{name: name, data: data}:
	default:
		c.pending.Done()
		c.Dropped.Add(1)
	}
}

// Flush waits for queued writes (tests, shutdown).
func (c *Cache) Flush() { c.pending.Wait() }

func (c *Cache) writer() {
	for w := range c.writes {
		c.store(w)
		c.pending.Done()
	}
}

func (c *Cache) store(w write) {
	p := c.path(w.name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return
	}
	buf := make([]byte, headerLen+len(w.data))
	binary.LittleEndian.PutUint32(buf[0:4], uint32(len(w.data)))
	binary.LittleEndian.PutUint32(buf[4:8], crc32.ChecksumIEEE(w.data))
	copy(buf[headerLen:], w.data)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, buf, 0o644); err != nil {
		os.Remove(tmp)
		return
	}
	if err := os.Rename(tmp, p); err != nil {
		os.Remove(tmp)
		return
	}
	c.mu.Lock()
	if _, ok := c.items[w.name]; !ok {
		c.items[w.name] = c.ll.PushFront(&item{name: w.name, size: int64(len(buf)), at: time.Now()})
		c.used += int64(len(buf))
		c.evictLocked()
	}
	c.mu.Unlock()
}

func (c *Cache) evictLocked() {
	for c.used > c.max && c.ll.Len() > 0 {
		el := c.ll.Back()
		it := el.Value.(*item)
		c.ll.Remove(el)
		delete(c.items, it.name)
		c.used -= it.size
		os.Remove(c.path(it.name))
	}
}

// Expire removes segments not read or written for longer than maxAge
// (cache expiry). Returns how many were removed.
func (c *Cache) Expire(maxAge time.Duration) int {
	if maxAge <= 0 {
		return 0
	}
	cutoff := time.Now().Add(-maxAge)
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for c.ll.Len() > 0 {
		el := c.ll.Back() // least recently used first
		it := el.Value.(*item)
		if it.at.After(cutoff) {
			break
		}
		c.ll.Remove(el)
		delete(c.items, it.name)
		c.used -= it.size
		os.Remove(c.path(it.name))
		n++
	}
	return n
}

func (c *Cache) drop(name string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[name]; ok {
		c.used -= el.Value.(*item).size
		c.ll.Remove(el)
		delete(c.items, name)
	}
	os.Remove(c.path(name))
}

// Usage returns (files, bytes) on disk.
func (c *Cache) Usage() (int, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ll.Len(), c.used
}

// Cleanup runs maintenance now: size eviction, expiry (maxAge > 0) and
// removal of files the index doesn't know (interrupted writes, leftovers).
// Returns segments expired, stray files removed and bytes freed.
func (c *Cache) Cleanup(maxAge time.Duration) (expired, stray int, freed int64) {
	c.mu.Lock()
	before := c.used
	c.evictLocked()
	c.mu.Unlock()
	expired = c.Expire(maxAge)
	c.mu.Lock()
	freed = before - c.used
	known := make(map[string]bool, len(c.items))
	for name := range c.items {
		known[name] = true
	}
	c.mu.Unlock()
	_ = filepath.WalkDir(c.dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if name := d.Name(); !known[name] {
			if info, err := d.Info(); err == nil && time.Since(info.ModTime()) > time.Minute { // not a write in progress
				if os.Remove(p) == nil {
					stray++
					freed += info.Size()
				}
			}
		}
		return nil
	})
	return expired, stray, freed
}

// Purge empties the disk cache. Readers that miss simply fetch again.
// Returns segments removed and bytes freed.
func (c *Cache) Purge() (int, int64) {
	c.mu.Lock()
	n, freed := c.ll.Len(), c.used
	for name := range c.items {
		os.Remove(c.path(name))
	}
	c.ll.Init()
	c.items = map[string]*list.Element{}
	c.used = 0
	c.mu.Unlock()
	return n, freed
}
