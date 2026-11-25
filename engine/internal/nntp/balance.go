package nntp

import (
	"context"
	"regexp"
	"sort"
	"strconv"
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
// Backups are never balanced. In both modes a provider throttled after "too many connections" goes
// last, so it isn't redialed on every request while others can serve.
func (c *Client) order(ctx context.Context, left []*pool, idx []int, prio Priority) []int {
	if len(idx) < 2 {
		return idx
	}
	// Backups keep list order in both modes: the first backup (often the
	// cheaper block account) is asked first, never a share by load.
	if c.LoadMode() != Balanced || left[idx[0]].p.Backup {
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
		usable -= pl.reserveLocked()
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
// opening connections beyond what it has open for this long. Then it
// widens by one connection every widenEvery until MaxConns, instead of
// jumping straight back to the full count and into the same refusals
// (Ether's learned limits). Vars for tests.
var (
	busyBackoff = 30 * time.Second
	widenEvery  = 5 * time.Second
)

// statedLimit finds a connection limit in a refusal's text: "connection
// limit (8)", "max 8 connections", "maximum number of connections (8)
// reached", "8 connections allowed". 0 when none is stated.
// A number after "limit"/"max" wins over one before "connections" ("you
// have 25 connections, limit 20" is 20).
var (
	limitAfterRE  = regexp.MustCompile(`(?i)\b(?:limit|max(?:imum)?)(?:\s+(?:number\s+)?(?:of\s+)?(?:connections?|conns?|sessions?))?(?:\s+(?:is|of|reached|exceeded|allowed))?\s*[:=(]?\s*(\d{1,4})\b`)
	limitBeforeRE = regexp.MustCompile(`(?i)\b(\d{1,4})\s*(?:concurrent\s+|simultaneous\s+)?connections`)
)

func statedLimit(msg string) int {
	for _, re := range []*regexp.Regexp{limitAfterRE, limitBeforeRE} {
		if m := re.FindStringSubmatch(msg); m != nil {
			if n, err := strconv.Atoi(m[1]); err == nil && n > 0 {
				return n
			}
		}
	}
	return 0
}

// throttle caps the pool at its current open connections for busyBackoff
// (the account's real limit is lower than MaxConns, or another program
// uses it). Called with the failed dial's slot already given back. A cap
// already in place is only extended during the hold, never lowered:
// several refusals in a row each give back a slot, and re-capping at the
// shrinking open count would walk the pool down to one connection.
//
// When the refusal states the account's limit below MaxConns, the pool
// keeps under it for the rest of its life (minus ~10% for the account's
// other users), ratcheting down only.
func (pl *pool) throttle(err error) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	if err != nil {
		if n := statedLimit(err.Error()); n > 0 && n < pl.p.MaxConns {
			n = max(n-n/10, 1)
			if pl.learned == 0 || n < pl.learned {
				pl.learned = n
			}
		}
	}
	now := time.Now()
	if pl.capConns == 0 || !now.Before(pl.capUntil) {
		// No cap, or one already widening: re-cap at what the server
		// actually carried.
		pl.capConns = max(pl.open, 1)
	}
	pl.capUntil = now.Add(busyBackoff)
}

// throttled reports whether a "too many connections" hold is on (while
// widening afterwards the pool is used normally).
func (pl *pool) throttled() bool {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	pl.maxConnsLocked() // clears a finished widening
	return pl.capConns > 0 && time.Now().Before(pl.capUntil)
}

// holdingLocked: a refusal's hold is on, so freed dial slots aren't passed
// to waiters (mu held).
func (pl *pool) holdingLocked() bool {
	return pl.capConns > 0 && time.Now().Before(pl.capUntil)
}

// maxConnsLocked is the connection limit right now: MaxConns, lowered to a
// learned server limit and while a refusal's cap holds or widens (mu held).
func (pl *pool) maxConnsLocked() int {
	limit := pl.p.MaxConns
	if pl.learned > 0 {
		limit = min(limit, pl.learned)
	}
	if pl.capConns > 0 {
		cap := pl.capConns
		if now := time.Now(); !now.Before(pl.capUntil) {
			cap += 1 + int(now.Sub(pl.capUntil)/widenEvery)
		}
		if cap < limit {
			return cap
		}
		pl.capConns = 0
	}
	return limit
}

// limits returns (learned server limit, limit in force now) for status.
func (pl *pool) limits() (learned, now int) {
	pl.mu.Lock()
	defer pl.mu.Unlock()
	return pl.learned, pl.maxConnsLocked()
}
