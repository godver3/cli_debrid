package nntp

import (
	"context"
	"errors"
	"runtime"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

// On Windows the clock moves in timer ticks, so a loopback fetch can
// measure 0 ms.
func TestSpeedTestOneProviderOnly(t *testing.T) {
	arts := nntptest.Post("st", "f.bin", awkward(40000), 4000)
	a := nntptest.Start(t, arts...)
	b := nntptest.Start(t) // has nothing
	c, _ := NewClient([]Provider{provider(a, "a", 4), provider(b, "b", 4)})
	defer c.Close()
	ids := make([]string, 0, len(arts)+1)
	for _, x := range arts {
		ids = append(ids, x.ID)
	}
	ids = append(ids, "missing@x")

	res, err := c.SpeedTest(context.Background(), "a", ids, 3, 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Articles != len(arts) || res.Missing != 1 || res.Bytes != 40000 || res.Error != "" || res.LatencyMs < 0 || (res.LatencyMs == 0 && runtime.GOOS != "windows") {
		t.Fatalf("a: %+v", res)
	}
	// b is asked alone: it has none of them, and a is never consulted
	res, err = c.SpeedTest(context.Background(), "b", ids, 3, 0)
	if err != nil || res.Articles != 0 || res.Error == "" {
		t.Fatalf("b: %+v %v", res, err)
	}
	if _, err := c.SpeedTest(context.Background(), "nope", ids, 3, 0); !errors.Is(err, ErrUnknownProvider) {
		t.Fatalf("unknown provider: %v", err)
	}
	// byte budget stops early
	res, _ = c.SpeedTest(context.Background(), "a", ids, 1, 8000)
	if res.Articles != 2 {
		t.Fatalf("budget: %+v", res)
	}
}
