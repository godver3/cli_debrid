package stream

import (
	"container/list"
	"context"
	"sync"
	"sync/atomic"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// Cache is an in-memory LRU of decoded segments, bounded by bytes, that
// also collapses concurrent fetches of the same segment into one request.
type Cache struct {
	disk     Tier // optional second tier, checked before the network
	mu       sync.Mutex
	max      int64
	used     int64
	ll       *list.List
	items    map[string]*list.Element
	inflight map[string]*call
}

type entry struct {
	key  string
	data []byte
}

type call struct {
	done    chan struct{}
	data    []byte
	err     error
	prio    nntp.Priority
	started atomic.Bool // the fetch holds a connection (left the queue)
}

// Tier is a slower cache behind memory (the disk cache).
type Tier interface {
	Get(key string) ([]byte, bool)
	Put(key string, data []byte)
}

// WithDisk adds a second tier: memory misses check it before fetching, and
// fetched segments are handed to it.
func (c *Cache) WithDisk(t Tier) *Cache {
	c.disk = t
	return c
}

// NewCache keeps up to maxBytes of decoded segment data.
func NewCache(maxBytes int64) *Cache {
	return &Cache{max: maxBytes, ll: list.New(), items: map[string]*list.Element{}, inflight: map[string]*call{}}
}

// Get returns a cached segment.
func (c *Cache) Get(key string) ([]byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		return el.Value.(*entry).data, true
	}
	return nil, false
}

// Do returns the cached segment, or runs fetch once for all concurrent
// callers and caches a successful result. fetch receives a context to pass
// to the NNTP client so the cache learns when the request leaves the queue.
//
// A Live caller joins an in-flight Prefetch fetch only once that fetch is
// on the wire. While it is still queued behind other prefetches the live
// caller fetches on its own: a rare duplicate download beats a stall.
func (c *Cache) Do(key string, prio nntp.Priority, fetch func(ctx context.Context) ([]byte, error)) ([]byte, error) {
	return c.DoCtx(context.Background(), key, prio, fetch)
}

// DoCtx is Do with a parent context for the fetch.
func (c *Cache) DoCtx(ctx context.Context, key string, prio nntp.Priority,
	fetch func(ctx context.Context) ([]byte, error)) ([]byte, error) {
	c.mu.Lock()
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		c.mu.Unlock()
		return el.Value.(*entry).data, nil
	}
	if cl, ok := c.inflight[key]; ok && (cl.prio >= prio || cl.started.Load()) {
		c.mu.Unlock()
		<-cl.done
		if cl.err == nil || cl.prio >= prio {
			return cl.data, cl.err
		}
		// A live read that joined a failed prefetch gets its own attempt.
		return c.DoCtx(ctx, key, prio, fetch)
	}
	cl := &call{done: make(chan struct{}), prio: prio}
	c.inflight[key] = cl // a live call replaces a prefetch one for later joiners
	c.mu.Unlock()

	if c.disk != nil {
		if d, ok := c.disk.Get(key); ok {
			cl.data = d
		}
	}
	if cl.data == nil {
		cl.data, cl.err = fetch(nntp.WithAcquireHook(ctx, func() { cl.started.Store(true) }))
		if cl.err == nil && c.disk != nil {
			c.disk.Put(key, cl.data)
		}
	}
	close(cl.done)

	c.mu.Lock()
	if c.inflight[key] == cl {
		delete(c.inflight, key)
	}
	if cl.err == nil {
		c.put(key, cl.data)
	}
	c.mu.Unlock()
	return cl.data, cl.err
}

// put stores data; caller holds mu.
func (c *Cache) put(key string, data []byte) {
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		return
	}
	// Budget by capacity, not length: the whole backing array stays in
	// memory (an NNTP body is read into an 800 KB buffer), so counting len
	// let the cache hold more than its budget.
	if int64(cap(data)) > c.max {
		return
	}
	c.items[key] = c.ll.PushFront(&entry{key: key, data: data})
	c.used += int64(cap(data))
	for c.used > c.max {
		el := c.ll.Back()
		e := el.Value.(*entry)
		c.ll.Remove(el)
		delete(c.items, e.key)
		c.used -= int64(cap(e.data))
	}
}

// Len returns (segments, bytes) currently cached.
func (c *Cache) Len() (int, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ll.Len(), c.used
}

// Put stores data fetched outside DoCtx (a streaming download) in memory
// and on disk.
func (c *Cache) Put(key string, data []byte) {
	c.mu.Lock()
	c.put(key, data)
	c.mu.Unlock()
	if c.disk != nil {
		c.disk.Put(key, data)
	}
}

// Started reports whether a fetch of key is already on the wire.
func (c *Cache) Started(key string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	cl, ok := c.inflight[key]
	return ok && cl.started.Load()
}

// Purge drops every cached segment from memory (fetches in flight finish
// normally). Returns segments and bytes dropped.
func (c *Cache) Purge() (int, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	n, b := c.ll.Len(), c.used
	c.ll.Init()
	c.items = map[string]*list.Element{}
	c.used = 0
	return n, b
}
