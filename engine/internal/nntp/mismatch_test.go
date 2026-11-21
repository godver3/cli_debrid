package nntp

import (
	"bytes"
	"context"
	"errors"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

// A Message-ID collision on one server (found live: Captain Marvel's first
// article came back with the wrong size) must not fail the read: the next
// server's copy is asked for, like cli_mount's ArticleMismatch failover.
func TestBodyAcceptSkipsAWrongCopy(t *testing.T) {
	right := awkward(4000)
	wrong := nntptest.Post("x", "other.bin", awkward(1500), 1500) // same id x.1@test, another file
	good := nntptest.Post("x", "right.bin", right, 4000)
	a := nntptest.Start(t, wrong...)
	b := nntptest.Start(t, good...)
	c, _ := NewClient([]Provider{provider(a, "a", 1), provider(b, "b", 1)})
	defer c.Close()

	accept := func(d []byte, m Meta) bool { return len(d) == len(right) }
	d, _, err := c.BodyAccept(context.Background(), Live, "x.1@test", accept)
	if err != nil || !bytes.Equal(d, right) {
		t.Fatalf("got %d bytes, %v; want the right copy from b", len(d), err)
	}
	// Plain Body keeps the first answer (callers that can't check).
	if d, _, _ := c.Body(context.Background(), Live, "x.1@test"); len(d) != 1500 {
		t.Fatalf("Body without a check: %d bytes", len(d))
	}
}

// Wrong on one server and missing on the other is ErrMismatch: not proof
// the article is missing, and never a retryable "inconclusive".
func TestBodyAcceptWrongEverywhereIsMismatch(t *testing.T) {
	wrong := nntptest.Post("x", "other.bin", awkward(1500), 1500)
	a := nntptest.Start(t, wrong...)
	b := nntptest.Start(t) // 430
	c, _ := NewClient([]Provider{provider(a, "a", 1), provider(b, "b", 1)})
	defer c.Close()

	_, _, err := c.BodyAccept(context.Background(), Live, "x.1@test", func([]byte, Meta) bool { return false })
	var inc *InconclusiveError
	if !errors.Is(err, ErrMismatch) || IsNotFound(err) || errors.As(err, &inc) {
		t.Fatalf("got %v; want ErrMismatch", err)
	}
}
