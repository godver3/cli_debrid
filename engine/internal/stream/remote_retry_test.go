package stream

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// countLink counts link refreshes.
type countLink struct {
	url       string
	refreshes atomic.Int32
}

func (c *countLink) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	if refresh {
		c.refreshes.Add(1)
	}
	return c.url, nil
}

// 429 and 5xx mean "again shortly": the read waits and retries the same
// link (cli_mount) instead of asking for a new one (AllDebrid suspends
// accounts that unlock a file too often) and failing.
func TestRemoteTooManyRequestsBacksOffWithoutNewLink(t *testing.T) {
	for _, mode := range []string{"chunk", "stream"} {
		t.Run(mode, func(t *testing.T) {
			file := bytes.Repeat([]byte("0123456789"), (2*RemoteChunk)/10)
			var reqs atomic.Int32
			serve := serveRanges(file, 0, nil)
			cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if reqs.Add(1) <= 2 {
					w.WriteHeader(http.StatusTooManyRequests)
					return
				}
				serve(w, r)
			}))
			defer cdn.Close()
			links := &countLink{url: cdn.URL}
			r := OpenRemote(links, nil, NewCache(256<<20), "R"+mode, int64(len(file)), ReadAhead{})
			buf := make([]byte, 4096)
			var err error
			if mode == "chunk" {
				var data []byte
				data, err = r.chunk(context.Background(), nntp.Live, 0)
				copy(buf, data)
			} else {
				_, err = r.ReadAtCtx(context.Background(), nntp.Live, buf, 0)
			}
			if err != nil || !bytes.Equal(buf, file[:len(buf)]) {
				t.Fatalf("read after two 429s: %v", err)
			}
			if n := links.refreshes.Load(); n != 0 {
				t.Fatalf("asked for %d new links on 429", n)
			}
		})
	}
}

// A CDN that sends headers and then nothing is cut off after the stall
// timeout and the read resumes on a new request, instead of hanging.
func TestRemoteStalledResponseIsRetried(t *testing.T) {
	old := remoteStall.Load()
	remoteStall.Store(int64(300 * time.Millisecond))
	t.Cleanup(func() { remoteStall.Store(old) })

	file := bytes.Repeat([]byte("abcdefghij"), (2*RemoteChunk)/10)
	var reqs atomic.Int32
	serve := serveRanges(file, 0, nil)
	release := make(chan struct{})
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if reqs.Add(1) == 1 {
			w.Header().Set("Content-Length", fmt.Sprint(len(file)))
			w.WriteHeader(http.StatusPartialContent)
			w.(http.Flusher).Flush()
			select { // never sends the body
			case <-r.Context().Done():
			case <-release:
			}
			return
		}
		serve(w, r)
	}))
	defer cdn.Close()
	defer close(release)
	r := OpenRemote(staticLink(cdn.URL), nil, NewCache(256<<20), "S", int64(len(file)), ReadAhead{})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	buf := make([]byte, 4096)
	start := time.Now()
	if _, err := r.ReadAtCtx(ctx, nntp.Live, buf, 0); err != nil || !bytes.Equal(buf, file[:len(buf)]) {
		t.Fatalf("read after a stall: %v", err)
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("took %s", d)
	}
}
