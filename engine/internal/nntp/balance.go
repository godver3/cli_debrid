package nntp

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"
)

// LoadMode says how requests are spread over the primary providers.
type LoadMode int

const (
	// FillFirst asks the primaries in list order and takes the first one
	// with a free connection: the first server carries the load until it is
	// full, the next ones only take overflow.
	FillFirst LoadMode = iota
	// Balanced asks the least-loaded primary first (busy connections over
	// the connections the request may use), rotating between equally loaded
	// ones, so every primary carries load in proportion to its connections.
	// Backups are never part of it.
	Balanced
)

// ParseLoadMode reads the config value ("fill" / "balanced"); anything else
// is FillFirst.
func ParseLoadMode(s string) LoadMode {
	if strings.EqualFold(strings.TrimSpace(s), "balanced") {
		return Balanced
	}
	return FillFirst
}

func (m LoadMode) String() string {
	if m == Balanced {
		return "balanced"
	}
	return "fill"
}

// SetLoadMode switches how primaries share requests. Safe at any time.
func (c *Client) SetLoadMode(m LoadMode) { c.mode.Store(int32(m)) }

// LoadMode returns the current mode.
func (c *Client) LoadMode() LoadMode { return LoadMode(c.mode.Load()) }

// order returns the indexes of idx (into left) in the order to try them.
// FillFirst keeps list order. Balanced sorts by load, starting the scan of
// equal loads at a rotating position, and puts providers the hint says
// keep missing this file's articles last (still asked, never skipped).
// In both modes a provider throttled after "too many connections" goes
// last, so it isn't redialed on every request while others can serve.
func (c *Client) order(ctx context.Context, left []*pool, idx []int, prio Priority) []int {
	if len(idx) < 2 {
		return idx
	}
	if c.LoadMode() != Balanced {
		var ok, throttled []int
		for _, i := range idx {
			if left[i].throttled() {
				throttled = append(throttled, i)
			} else {
				ok = append(ok, i)
			}
		}
		if len(throttled) == 0 {
			return idx
		}
		return append(ok, throttled...)
	}
	out := make([]int, len(idx))
	start := int(c.rr.Add(1) % uint64(len(idx)))
	for i := range idx {
		out[i] = idx[(start+i)%len(idx)]
	}
	type load struct {
		busy, usable int
		throttled    bool
	}
	loads := make(map[int]load, len(out))
	for _, i := range out {
		b, u := left[i].load(prio)
		loads[i] = load{b, u, left[i].throttled()}
	}
	hint := missHintFrom(ctx)
	sort.SliceStable(out, func(a, b int) bool {
		pa, pb := left[out[a]], left[out[b]]
		if ta, tb := loads[out[a]].throttled, loads[out[b]].throttled; ta != tb {
			return tb
		}
		if hint != nil {
			da, db := hint.demoted(pa.p.Name), hint.demoted(pb.p.Name)
			if da != db {
				return db
			}
		}
		la, lb := loads[out[a]], loads[out[b]]
		// busy/usable, compared without division.
		return la.busy*lb.usable < lb.busy*la.usable
	})
	return out
}

// load returns (busy connections, connections prio may use) for ordering.
func (pl *pool) load(prio Priority) (busy, usable int) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	busy = pl.open - len(pl.idle)
	usable = pl.maxConnsLocked()
	if prio != Live {
		usable -= pl.reserve()
	}
	return busy, max(usable, 1)
}

// missDemote: after this many "no such article" answers for one file, a
// provider is asked last for that file's articles (Balanced only). A
// server with shorter retention then stops costing a round trip on every
// segment of an old post. Proof is unchanged: it is still asked.
const missDemote = 3

// MissHint remembers, for one file, which providers didn't have its
// articles. Safe for concurrent use; the zero value is ready.
type MissHint struct {
	mu     sync.Mutex
	misses map[string]int
}

// NewMissHint returns an empty hint.
func NewMissHint() *MissHint { return &MissHint{} }

func (h *MissHint) miss(name string) {
	h.mu.Lock()
	if h.misses == nil {
		h.misses = map[string]int{}
	}
	h.misses[name]++
	h.mu.Unlock()
}

// hit lets a provider that delivered work its way back up.
func (h *MissHint) hit(name string) {
	h.mu.Lock()
	if h.misses[name] > 0 {
		h.misses[name]--
	}
	h.mu.Unlock()
}

func (h *MissHint) demoted(name string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.misses[name] >= missDemote
}

type missHintKey struct{}

// WithMissHint attaches a file's hint to the requests made with ctx.
func WithMissHint(ctx context.Context, h *MissHint) context.Context {
	if h == nil {
		return ctx
	}
	return context.WithValue(ctx, missHintKey{}, h)
}

func missHintFrom(ctx context.Context) *MissHint {
	h, _ := ctx.Value(missHintKey{}).(*MissHint)
	return h
}

// busyBackoff: after a "too many connections" answer the pool stops
// opening connections beyond what it has open for this long.
const busyBackoff = 30 * time.Second

// throttle caps the pool at its current open connections for busyBackoff
// (the account's real limit is lower than MaxConns, or another program
// uses it). Called with the failed dial's slot already given back.
func (pl *pool) throttle() {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	pl.capConns = max(pl.open, 1)
	pl.capUntil = time.Now().Add(busyBackoff)
}

// throttled reports whether a "too many connections" backoff is on.
func (pl *pool) throttled() bool {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	pl.maxConnsLocked() // clears an expired backoff
	return pl.capConns > 0
}

// maxConnsLocked is MaxConns, lowered while a throttle is on (mu held).
func (pl *pool) maxConnsLocked() int {
	if pl.capConns > 0 {
		if time.Now().Before(pl.capUntil) {
			return min(pl.capConns, pl.p.MaxConns)
		}
		pl.capConns = 0
	}
	return pl.p.MaxConns
}
