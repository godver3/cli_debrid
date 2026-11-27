package stream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
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
// serverUnreachable: the request never reached the link's server (refused,
// no route, host gone). Retrying the same link can't help when that CDN
// server is down: found live, TorBox's nexus-405.ecan refused every
// connection for hours while the engine kept its cached link (6 h). A fresh
// link lets the service mint one on a server that works (TorBox: another
// region's CDN).
func serverUnreachable(status int, err error) bool {
	if status != 0 || err == nil {
		return false
	}
	if errors.Is(err, syscall.ECONNREFUSED) || errors.Is(err, syscall.EHOSTUNREACH) || errors.Is(err, syscall.ENETUNREACH) {
		return true
	}
	var dns *net.DNSError
	if errors.As(err, &dns) && dns.IsNotFound {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "connection refused") || strings.Contains(msg, "actively refused") // Windows wording
}

// checkContentRange validates the byte positions before either reader stores
// data from a partial response.
func checkContentRange(resp *http.Response, start, end int64) error {
	header := resp.Header.Get("Content-Range")
	unit, span, _ := strings.Cut(header, " ")
	bounds, total, _ := strings.Cut(span, "/")
	first, last, _ := strings.Cut(bounds, "-")
	a, aErr := strconv.ParseUint(first, 10, 64)
	b, bErr := strconv.ParseUint(last, 10, 64)
	size, sizeErr := strconv.ParseUint(total, 10, 64)
	if strings.EqualFold(unit, "bytes") && aErr == nil && bErr == nil && a == uint64(start) && b == uint64(end) &&
		(total == "*" || (sizeErr == nil && size > uint64(end))) &&
		(resp.ContentLength < 0 || resp.ContentLength == end-start+1) {
		return nil
	}
	return fmt.Errorf("%w: invalid Content-Range %q (Content-Length %d) for bytes %d-%d",
		ErrLinkRefused, header, resp.ContentLength, start, end)
}

// refreshUnreachable carries the failed URL to link sources that can use it.
func refreshUnreachable(ctx context.Context, links LinkSource, ref, failedURL string) (string, error) {
	if source, ok := links.(interface {
		RefreshUnreachable(context.Context, string, string) (string, error)
	}); ok {
		return source.RefreshUnreachable(ctx, ref, failedURL)
	}
	return links.Link(ctx, ref, true)
}

type unreachableLinkError struct {
	url string
	err error
}

func (e *unreachableLinkError) Error() string { return e.err.Error() }
func (e *unreachableLinkError) Unwrap() error { return e.err }

func badLinkResponse(status int, err error) bool {
	return (status >= 400 && status < 500 && !transientStatus(status)) ||
		(status == http.StatusPartialContent && errors.Is(err, ErrLinkRefused))
}

func transientStatus(code int) bool {
	switch code {
	case http.StatusTooManyRequests, http.StatusInternalServerError, http.StatusBadGateway,
		http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return false
}
