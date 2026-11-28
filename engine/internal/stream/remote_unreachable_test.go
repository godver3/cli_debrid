package stream

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// switchLink hands out a dead server's link until refreshed, then a working one.
type switchLink struct {
	dead, live string
	refreshes  atomic.Int32
}

func (s *switchLink) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	if refresh {
		return "", fmt.Errorf("unreachable server reported as a plain refresh")
	}
	if s.refreshes.Load() > 0 {
		return s.live, nil
	}
	return s.dead, nil
}

func (s *switchLink) RefreshUnreachable(_ context.Context, _, failedURL string) (string, error) {
	if failedURL != s.dead {
		return "", fmt.Errorf("unreachable URL = %q, want %q", failedURL, s.dead)
	}
	s.refreshes.Add(1)
	return s.live, nil
}

// A link whose CDN server refuses connections gets one fresh link (the
// service may mint it on another server) instead of retrying the dead one
// until the cached link expires. Found live: TorBox nexus-405.ecan.
func TestRemoteRefusedServerGetsAFreshLink(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	deadURL := "http://" + ln.Addr().String() + "/f"
	ln.Close() // nothing listens there now: connection refused

	for _, mode := range []string{"chunk", "stream"} {
		t.Run(mode, func(t *testing.T) {
			file := bytes.Repeat([]byte("0123456789"), (2*RemoteChunk)/10)
			cdn := httptest.NewServer(http.HandlerFunc(serveRanges(file, 0, nil)))
			defer cdn.Close()
			links := &switchLink{dead: deadURL, live: cdn.URL}
			r := OpenRemote(links, nil, NewCache(256<<20), "U"+mode, int64(len(file)), ReadAhead{})
			buf := make([]byte, 4096)
			if mode == "chunk" {
				var data []byte
				data, err = r.chunk(context.Background(), nntp.Live, 0)
				copy(buf, data)
			} else {
				_, err = r.ReadAtCtx(context.Background(), nntp.Live, buf, 0)
			}
			if err != nil || !bytes.Equal(buf, file[:len(buf)]) {
				t.Fatalf("read after a refused server: %v", err)
			}
			if n := links.refreshes.Load(); n != 1 {
				t.Fatalf("asked for %d new links, want 1", n)
			}
		})
	}
}

func TestServerUnreachable(t *testing.T) {
	if serverUnreachable(503, &net.OpError{Op: "dial", Err: errString("connection refused")}) {
		t.Fatal("an HTTP answer is not unreachable")
	}
	if !serverUnreachable(0, &net.DNSError{Name: "x", IsNotFound: true}) {
		t.Fatal("host gone should count")
	}
	if serverUnreachable(0, errString("connection reset by peer")) {
		t.Fatal("a reset is transient, not unreachable")
	}
}

type errString string

func (e errString) Error() string { return string(e) }
