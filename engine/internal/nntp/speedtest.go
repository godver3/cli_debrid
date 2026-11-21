package nntp

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// SpeedResult is one provider's manual speed test.
type SpeedResult struct {
	Provider  string  `json:"provider"`
	Bytes     int64   `json:"bytes"`
	Articles  int     `json:"articles"`
	Missing   int     `json:"missing"`
	Seconds   float64 `json:"seconds"`
	MBps      float64 `json:"mb_per_second"`
	LatencyMs float64 `json:"latency_ms"` // fastest single article (connection ready)
	Error     string  `json:"error,omitempty"`
}

// ErrUnknownProvider: no provider by that name.
var ErrUnknownProvider = errors.New("nntp: unknown provider")

// SpeedTest downloads ids from one provider only, on up to conns
// connections, until maxBytes are read, the ids run out or ctx ends. It
// runs at prefetch priority, so it never takes the playback reserve.
func (c *Client) SpeedTest(ctx context.Context, provider string, ids []string, conns int, maxBytes int64) (SpeedResult, error) {
	var pl *pool
	for _, p := range c.pools {
		if p.p.Name == provider {
			pl = p
		}
	}
	if pl == nil {
		return SpeedResult{}, fmt.Errorf("%w: %s", ErrUnknownProvider, provider)
	}
	res := SpeedResult{Provider: provider}
	if len(ids) == 0 {
		return res, errors.New("nntp: no articles to test with")
	}
	conns = min(max(conns, 1), max(pl.p.MaxConns-pl.reserve(), 1))
	var (
		mu         sync.Mutex
		next       int
		lastErr    error
		wg         sync.WaitGroup
		latencySet bool // a real 0 ms can happen (Windows timer ticks)
	)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	take := func() (string, bool) {
		mu.Lock()
		defer mu.Unlock()
		if next >= len(ids) || (maxBytes > 0 && res.Bytes >= maxBytes) {
			return "", false
		}
		next++
		return ids[next-1], true
	}
	start := time.Now()
	for i := 0; i < conns; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				id, ok := take()
				if !ok || ctx.Err() != nil {
					return
				}
				cn, err := pl.acquire(ctx, Prefetch)
				if err != nil {
					mu.Lock()
					if ctx.Err() == nil {
						lastErr = err
					}
					mu.Unlock()
					return
				}
				began := time.Now()
				data, _, err := cn.body(id)
				took := time.Since(began)
				pl.record(began, cn.lastBytes, err == nil, err)
				var pe *ProtocolError
				switch {
				case err == nil:
					pl.release(cn)
				case IsNotFound(err) || errors.Is(err, ErrTruncated) || errors.As(err, &pe):
					pl.release(cn)
				default:
					pl.discard(cn)
				}
				mu.Lock()
				switch {
				case err == nil:
					res.Bytes += int64(len(data))
					res.Articles++
					if ms := float64(took.Microseconds()) / 1000; !latencySet || ms < res.LatencyMs {
						res.LatencyMs, latencySet = ms, true
					}
				case IsNotFound(err) || errors.Is(err, ErrTruncated):
					res.Missing++
				default:
					lastErr = err
				}
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	res.Seconds = time.Since(start).Seconds()
	if res.Seconds > 0 {
		res.MBps = float64(res.Bytes) / (1 << 20) / res.Seconds
	}
	c.bytes.Add(res.Bytes)
	c.articles.Add(int64(res.Articles))
	if res.Articles == 0 {
		if lastErr == nil {
			lastErr = errors.New("none of the test articles were on this server")
		}
		res.Error = lastErr.Error()
		return res, nil
	}
	if lastErr != nil {
		res.Error = lastErr.Error()
	}
	return res, nil
}
