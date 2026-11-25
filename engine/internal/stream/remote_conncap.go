package stream

import (
	"context"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Per-host connection cap, TorBox only (Ether's conncap): TorBox's CDN
// counts connections, not requests, answering the 21st open connection to
// one host with HTTP 429 and a five-minute pause for that whole host, idle
// keep-alive connections included. Other services spread files over many
// hosts and aren't capped. Every debrid client shares one cap per TorBox
// host:port; a dial over it first asks the transports to drop their idle
// connections, then waits for one to close.

// RemoteConnsPerHost caps open connections per TorBox CDN host (<= 0 = no cap).
var RemoteConnsPerHost atomic.Int64

func init() { RemoteConnsPerHost.Store(16) }

// cappedHost: the hosts the cap applies to (TorBox's CDN). A var for tests.
var cappedHost = func(host string) bool {
	host = strings.ToLower(host)
	for _, d := range []string{"tb-cdn.st", "torbox.app"} {
		if host == d || strings.HasSuffix(host, "."+d) {
			return true
		}
	}
	return false
}

type connCap struct {
	mu    sync.Mutex
	open  map[string]int
	wake  chan struct{}
	idle  []func() // CloseIdleConnections of every transport using the cap
	limit func() int
}

var sharedCap = &connCap{open: map[string]int{}, wake: make(chan struct{}),
	limit: func() int { return int(RemoteConnsPerHost.Load()) }}

func (c *connCap) register(closeIdle func()) {
	c.mu.Lock()
	c.idle = append(c.idle, closeIdle)
	c.mu.Unlock()
}

func (c *connCap) acquire(ctx context.Context, addr string) error {
	asked := false
	for {
		c.mu.Lock()
		if max := c.limit(); max <= 0 || c.open[addr] < max {
			c.open[addr]++
			c.mu.Unlock()
			return nil
		}
		wake, idle := c.wake, c.idle
		c.mu.Unlock()
		if !asked {
			// Idle keep-alive connections count against the host's limit:
			// let them go before waiting on busy ones.
			for _, f := range idle {
				f()
			}
			asked = true
			continue
		}
		t := time.NewTimer(time.Second) // re-check: a closed idle conn may not wake us
		select {
		case <-wake:
		case <-t.C:
			asked = false
		case <-ctx.Done():
			t.Stop()
			return ctx.Err()
		}
		t.Stop()
	}
}

func (c *connCap) release(addr string) {
	c.mu.Lock()
	if c.open[addr] > 0 {
		c.open[addr]--
	}
	if c.open[addr] == 0 {
		delete(c.open, addr)
	}
	close(c.wake)
	c.wake = make(chan struct{})
	c.mu.Unlock()
}

// inUse is the open connection count for addr (tests).
func (c *connCap) inUse(addr string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.open[addr]
}

type dialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

func (c *connCap) wrap(next dialFunc) dialFunc {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		if host, _, err := net.SplitHostPort(addr); err != nil || !cappedHost(host) {
			return next(ctx, network, addr)
		}
		if err := c.acquire(ctx, addr); err != nil {
			return nil, err
		}
		conn, err := next(ctx, network, addr)
		if err != nil {
			c.release(addr)
			return nil, err
		}
		return &cappedConn{Conn: conn, release: func() { c.release(addr) }}, nil
	}
}

type cappedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *cappedConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.release)
	return err
}
