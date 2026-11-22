package stream

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"
)

// Debrid HTTP limits (cli_mount: 30 s dial and response headers, a 90 s
// no-progress kicker, 120 s per mount read). Without them a CDN connection
// that stops sending holds a mount read until the player gives up.
var (
	remoteDialTimeout   = 30 * time.Second
	remoteHeaderTimeout = 30 * time.Second
)

// remoteStall: a request (headers or body) that delivers no bytes for this
// long is cancelled and retried like a dropped one (atomic: tests change it
// while downloaders run).
var remoteStall atomic.Int64

func init() { remoteStall.Store(int64(30 * time.Second)) }

func remoteStallTimeout() time.Duration { return time.Duration(remoteStall.Load()) }

// defaultRemoteClient is used when neither the caller nor the LinkSource
// gives a client.
var defaultRemoteClient = NewRemoteClient(nil)

// NewRemoteClient returns an HTTP client for debrid downloads with the
// timeouts above; proxy may be nil (then the environment's proxy is used).
// Without a proxy its TorBox connections count against the shared cap.
func NewRemoteClient(proxy func(*http.Request) (*url.URL, error)) *http.Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	dial := (&net.Dialer{Timeout: remoteDialTimeout, KeepAlive: 30 * time.Second}).DialContext
	if proxy == nil {
		// TorBox's per-host connection cap (remote_conncap.go). Through a
		// proxy every dial goes to the proxy, not the CDN host, so it can't
		// be applied there.
		dial = sharedCap.wrap(dial)
		sharedCap.register(tr.CloseIdleConnections)
	}
	tr.DialContext = dial
	tr.ResponseHeaderTimeout = remoteHeaderTimeout
	tr.TLSHandshakeTimeout = 15 * time.Second
	tr.IdleConnTimeout = 30 * time.Second // idle connections count against TorBox's limit
	if proxy != nil {
		tr.Proxy = proxy
	}
	return &http.Client{Transport: tr}
}

// watchdog cancels a request that makes no progress for remoteStallTimeout.
// Start it before the request is sent; call progress on every read that
// returned bytes; stop when done. stalled reports whether it fired.
type watchdog struct {
	t      *time.Timer
	fired  chan struct{}
	cancel context.CancelFunc
}

func newWatchdog(parent context.Context) (context.Context, *watchdog) {
	ctx, cancel := context.WithCancel(parent)
	w := &watchdog{fired: make(chan struct{}), cancel: cancel}
	w.t = time.AfterFunc(remoteStallTimeout(), func() {
		close(w.fired)
		cancel()
	})
	return ctx, w
}

func (w *watchdog) progress() { w.t.Reset(remoteStallTimeout()) }

// pause stops the clock while the caller waits on purpose (a paused stream
// holding its connection); resume restarts it.
func (w *watchdog) pause()  { w.t.Stop() }
func (w *watchdog) resume() { w.t.Reset(remoteStallTimeout()) }

func (w *watchdog) stop() {
	w.t.Stop()
	w.cancel()
}

func (w *watchdog) stalled() bool {
	select {
	case <-w.fired:
		return true
	default:
		return false
	}
}

// progressReader feeds a watchdog from a response body.
type progressReader struct {
	r io.Reader
	w *watchdog
}

func (p progressReader) Read(b []byte) (int, error) {
	n, err := p.r.Read(b)
	if n > 0 {
		p.w.progress()
	}
	return n, err
}

// transientStatus: answers that say "try again shortly", not "this link is
// bad". cli_mount backs off on these and keeps the link; asking for a new
// link on a 429 only adds load (and AllDebrid suspends accounts that
// unlock the same file too often).
func transientStatus(code int) bool {
	switch code {
	case http.StatusTooManyRequests, http.StatusInternalServerError, http.StatusBadGateway,
		http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return false
}
