package stream

import (
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// RemoteProviderStats is what the Mount page shows per debrid provider,
// next to the usenet server table.
type RemoteProviderStats struct {
	Provider   string  `json:"provider"` // ref prefix: rd, tb, ad, pm, dl, hw
	Open       int64   `json:"open"`     // HTTP downloads on the wire now
	Requests   int64   `json:"requests"`
	Bytes      int64   `json:"bytes"`
	Errors     int64   `json:"errors"`
	LinkErrors int64   `json:"link_errors"`
	LatencyMs  float64 `json:"latency_ms"` // average time to response headers
	LastError  string  `json:"last_error,omitempty"`
}

type remoteCounters struct {
	open, requests, bytes, errors, linkErrors atomic.Int64
	latencyNs, latencyN                       atomic.Int64
	lastErr                                   atomic.Value // string
}

var remoteStats sync.Map // provider -> *remoteCounters

// remoteProvider is the provider part of a debrid ref ("rd:ID:3" -> "rd").
func remoteProvider(ref string) string {
	if i := strings.IndexByte(ref, ':'); i > 0 {
		return ref[:i]
	}
	return "debrid"
}

func countersFor(ref string) *remoteCounters {
	p := remoteProvider(ref)
	if v, ok := remoteStats.Load(p); ok {
		return v.(*remoteCounters)
	}
	v, _ := remoteStats.LoadOrStore(p, &remoteCounters{})
	return v.(*remoteCounters)
}

func (c *remoteCounters) begin() { c.open.Add(1); c.requests.Add(1) }
func (c *remoteCounters) end()   { c.open.Add(-1) }
func (c *remoteCounters) headers(since time.Time) {
	c.latencyNs.Add(int64(time.Since(since)))
	c.latencyN.Add(1)
}
func (c *remoteCounters) fail(err error) {
	if err == nil {
		return
	}
	c.errors.Add(1)
	c.lastErr.Store(err.Error())
}
func (c *remoteCounters) linkFail(err error) {
	if err == nil {
		return
	}
	c.linkErrors.Add(1)
	c.lastErr.Store(err.Error())
}

// RemoteStats returns the debrid counters per provider, sorted by name.
func RemoteStats() []RemoteProviderStats {
	out := []RemoteProviderStats{}
	remoteStats.Range(func(k, v any) bool {
		c := v.(*remoteCounters)
		s := RemoteProviderStats{Provider: k.(string), Open: max(0, c.open.Load()), Requests: c.requests.Load(),
			Bytes: c.bytes.Load(), Errors: c.errors.Load(), LinkErrors: c.linkErrors.Load()}
		if n := c.latencyN.Load(); n > 0 {
			s.LatencyMs = float64(c.latencyNs.Load()) / float64(n) / 1e6
		}
		if e, ok := c.lastErr.Load().(string); ok {
			s.LastError = e
		}
		out = append(out, s)
		return true
	})
	sort.Slice(out, func(i, j int) bool { return out[i].Provider < out[j].Provider })
	return out
}

// resetRemoteStats is for tests.
func resetRemoteStats() {
	remoteStats.Range(func(k, _ any) bool { remoteStats.Delete(k); return true })
}
