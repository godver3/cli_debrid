package stream

import (
	"container/list"
	"context"
	"strings"
	"sync"
	"sync/atomic"
	"time"

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

	// fetchNs: moving average of how long an article fetch takes once on
	// the wire, for the live hedge (see hedgeAfter).
	fetchNs atomic.Int64
	// Hedges counts live reads that stopped waiting for a slow background
	// fetch and asked themselves; HedgeWins how many of those finished first.
	Hedges, HedgeWins atomic.Int64
}

type entry struct {
	key  string
	data []byte
}

type call struct {
	done      chan struct{}
	data      []byte
	err       error
	prio      nntp.Priority
	started   atomic.Bool  // the fetch holds a connection (left the queue)
	startedAt atomic.Int64 // unix ns it got there (or reached the head of a pipeline)
}

func (cl *call) markStarted() {
	if !cl.started.Swap(true) {
		cl.startedAt.Store(time.Now().UnixNano())
	}
}

// Live hedge (Ether's liveHedgeDelay): a playback read that joined a
// background fetch already on the wire stops waiting for it after
// hedgeAfter and fetches the article itself; whichever answer comes first
// is used. Before, it waited for the background fetch however long it
// took, up to a provider's whole 30 s timeout on a stalled connection.
// The wait scales with how long fetches normally take, so an ordinary
// fetch on a full line is not downloaded twice. Vars for tests.
var (
	hedgeMin    = 400 * time.Millisecond
	hedgeMax    = 8 * time.Second
	hedgeFactor = int64(3)
)

// hedgeAfter is how long a live read waits on a joined background fetch.
func (c *Cache) hedgeAfter() time.Duration {
	return min(max(time.Duration(c.fetchNs.Load()*hedgeFactor), hedgeMin), hedgeMax)
}

func (c *Cache) noteFetch(cl *call) {
	at := cl.startedAt.Load()
	if at == 0 {
		return
	}
	d := time.Now().UnixNano() - at
	if old := c.fetchNs.Load(); old == 0 {
		c.fetchNs.Store(d)
	} else {
		c.fetchNs.Store(old + (d-old)/8)
	}
}

// hedgeable: usenet articles only. Debrid chunks share the cache but are
// fetched (and stall) differently.
func hedgeable(key string) bool { return !strings.HasPrefix(key, "remote:") }

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
// on the wire, and then only for hedgeAfter (see there). While it is still
// queued behind other prefetches the live caller fetches on its own: a
// rare duplicate download beats a stall.
func (c *Cache) Do(key string, prio nntp.Priority, fetch func(ctx context.Context) ([]byte, error)) ([]byte, error) {
	return c.DoCtx(context.Background(), key, prio, fetch)
}

// DoCtx is Do with a parent context for the fetch.
func (c *Cache) DoCtx(ctx context.Context, key string, prio nntp.Priority,
	fetch func(ctx context.Context) ([]byte, error)) ([]byte, error) {
	return c.doCtx(ctx, key, prio, fetch, true)
}

func (c *Cache) doCtx(ctx context.Context, key string, prio nntp.Priority,
	fetch func(ctx context.Context) ([]byte, error), joinLower bool) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	c.mu.Lock()
	if err := ctx.Err(); err != nil {
		c.mu.Unlock()
		return nil, err
	}
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		c.mu.Unlock()
		return el.Value.(*entry).data, nil
	}
	if cl, ok := c.inflight[key]; ok && (cl.prio >= prio || (joinLower && cl.started.Load())) {
		c.mu.Unlock()
		if cl.prio < prio && hedgeable(key) {
			return c.joinOrHedge(ctx, key, prio, fetch, cl)
		}
		data, err := waitCall(ctx, cl)
		if err == nil || cl.prio >= prio {
			return data, err
		}
		// Try once at our priority after a failed lower-priority fetch.
		return c.doCtx(ctx, key, prio, fetch, false)
	}
	cl := &call{done: make(chan struct{}), prio: prio}
	c.inflight[key] = cl // a live call replaces a prefetch one for later joiners
	c.mu.Unlock()

	if c.disk != nil {
		if d, ok := c.disk.Get(key); ok {
			cl.data = d
		}
	}
	fetched := false
	if err := ctx.Err(); err != nil {
		cl.err = err
	} else if cl.data == nil {
		cl.data, cl.err = fetch(nntp.WithAcquireHook(ctx, cl.markStarted))
		fetched = cl.err == nil
	}
	c.complete(key, cl, fetched)
	return waitCall(ctx, cl)
}

func waitCall(ctx context.Context, cl *call) ([]byte, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-cl.done:
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return cl.data, cl.err
	}
}

// complete publishes a finished call: memory, disk (fetched: it came from
// the network) and its waiters. Exactly once per call.
func (c *Cache) complete(key string, cl *call, fetched bool) {
	if fetched {
		if hedgeable(key) {
			c.noteFetch(cl)
		}
		if c.disk != nil {
			c.disk.Put(key, cl.data)
		}
	}
	// Unlist (and cache) before waking waiters: one that retries on
	// wake-up (a live read whose prefetch failed) must not find this
	// finished call still in flight and spin on it.
	c.mu.Lock()
	if c.inflight[key] == cl {
		delete(c.inflight, key)
	}
	if cl.err == nil {
		c.put(key, cl.data)
	}
	c.mu.Unlock()
	close(cl.done)
}

// joinOrHedge waits on a lower-priority fetch already on the wire for up
// to hedgeAfter from when it started, then fetches the article itself.
func (c *Cache) joinOrHedge(ctx context.Context, key string, prio nntp.Priority,
	fetch func(ctx context.Context) ([]byte, error), old *call) ([]byte, error) {
	wait := c.hedgeAfter()
	if at := old.startedAt.Load(); at != 0 {
		wait -= time.Since(time.Unix(0, at))
	}
	if wait > 0 {
		t := time.NewTimer(wait)
		select {
		case <-old.done:
			t.Stop()
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			if old.err == nil {
				return old.data, nil
			}
			return c.doCtx(ctx, key, prio, fetch, false)
		case <-ctx.Done():
			t.Stop()
			return nil, ctx.Err()
		case <-t.C:
		}
	}
	c.mu.Lock()
	if err := ctx.Err(); err != nil {
		c.mu.Unlock()
		return nil, err
	}
	if el, ok := c.items[key]; ok {
		c.ll.MoveToFront(el)
		c.mu.Unlock()
		return el.Value.(*entry).data, nil
	}
	if cur, ok := c.inflight[key]; ok && cur != old && cur.prio >= prio {
		// Another live read already hedged: share its fetch.
		c.mu.Unlock()
		return waitCall(ctx, cur)
	}
	own := &call{done: make(chan struct{}), prio: prio}
	c.inflight[key] = own
	c.mu.Unlock()
	c.Hedges.Add(1)

	type result struct {
		data []byte
		err  error
	}
	res := make(chan result, 1)
	hctx, hcancel := context.WithCancel(ctx)
	defer hcancel() // the slow fetch won: stop downloading the article twice
	go func() {
		d, err := fetch(nntp.WithAcquireHook(hctx, own.markStarted))
		res <- result{d, err}
	}()
	fetched := false
	select {
	case r := <-res:
		own.data, own.err = r.data, r.err
		fetched = r.err == nil
		if fetched {
			c.HedgeWins.Add(1)
		}
	case <-old.done:
		if old.err == nil {
			own.data = old.data // the slow fetch made it after all
		} else {
			select {
			case r := <-res:
				own.data, own.err = r.data, r.err
				fetched = r.err == nil
			case <-ctx.Done():
				own.err = ctx.Err()
			}
		}
	case <-ctx.Done():
		own.err = ctx.Err()
	}
	c.complete(key, own, fetched)
	return waitCall(ctx, own)
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
