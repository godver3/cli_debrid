package engine

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// realMKV makes a short real Matroska file with ffmpeg (skips without it).
func realMKV(t *testing.T) []byte {
	t.Helper()
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		t.Skip("ffmpeg not installed")
	}
	if _, err := exec.LookPath("ffprobe"); err != nil {
		t.Skip("ffprobe not installed")
	}
	out := filepath.Join(t.TempDir(), "a.mkv")
	cmd := exec.Command("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi",
		"-i", "sine=frequency=440", "-t", "3", "-c:v", "mpeg4", "-c:a", "mp2", out)
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Skipf("ffmpeg failed: %v %s", err, b)
	}
	b, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// cli_mount's ffprobe verdicts, through the loopback server.
func TestMediaProbeInHealthChecks(t *testing.T) {
	good := realMKV(t)
	s := nntptest.Start(t)
	e := newEngine(t, s, func(c *Config) { c.MediaProbe = true })
	garbage := append([]byte{0x1A, 0x45, 0xDF, 0xA3}, data(3, len(good))...) // right header, nothing playable
	for _, c := range []struct {
		name, want, reason string
		body               []byte
		drop               string
	}{
		{"Good", health.Healthy, "", good, ""},
		{"Garbage", health.Broken, ReasonProbeFailed, garbage, ""},
		{"Missing", health.Broken, ReasonSegmentMissing, good, ".2@test"},
	} {
		nz := nntptest.PostNZB(s, c.name, nntptest.File{Name: c.name + ".mkv", Data: c.body})
		j, _ := e.Submit(nz, c.name, "movies", "")
		if got := waitDone(t, e, j.ID); got.Status != store.Ready {
			t.Fatalf("%s: %s %s", c.name, got.Status, got.Error)
		}
		if c.drop != "" {
			s.Remove(c.name + "0" + c.drop) // an article ffprobe will read
		}
		e.CheckHealth(j.ID, false)
		h := waitHealth(t, e, j.ID)
		if h.Status != c.want || (c.reason != "" && (len(h.Broken) == 0 || h.Broken[0].Reason != c.reason)) {
			t.Fatalf("%s: %+v", c.name, h)
		}
	}
}

func TestMediaProbeOffByDefault(t *testing.T) {
	good := realMKV(t)
	s := nntptest.Start(t)
	e := newEngine(t, s, nil)
	garbage := append([]byte{0x1A, 0x45, 0xDF, 0xA3}, data(4, len(good))...)
	nz := nntptest.PostNZB(s, "g", nntptest.File{Name: "G.mkv", Data: garbage})
	j, _ := e.Submit(nz, "G", "movies", "")
	waitDone(t, e, j.ID)
	e.CheckHealth(j.ID, false)
	if h := waitHealth(t, e, j.ID); h.Status != health.Healthy {
		t.Fatalf("probe ran with media_probe off: %+v", h)
	}
}

func TestClassifyProbeLikeCliMount(t *testing.T) {
	run := func(script string) ([]byte, error) { return exec.Command("sh", "-c", script).Output() }
	ctx := context.Background()
	for script, want := range map[string]probeResult{
		`echo '{"streams":[{"codec_type":"video"}]}'`:         {state: "healthy"},
		`echo '{"streams":[{"codec_type":"subtitle"}]}'`:      {state: "broken", reason: ReasonNoPlayable},
		`echo 'Invalid data found' >&2; exit 1`:               {state: "broken", reason: ReasonProbeFailed},
		`echo 'Connection timed out' >&2; exit 1`:             {state: "unknown", reason: reasonProbeTimeout},
		`echo 'Server returned 5XX Server Error' >&2; exit 1`: {state: "unknown", reason: reasonProbeUnavailble},
		`echo 'not json'`: {state: "unknown", reason: reasonProbeUnavailble},
	} {
		out, err := run(script)
		if got := classifyProbe(ctx, out, err); got != want {
			t.Errorf("%s: %+v, want %+v", script, got, want)
		}
	}
	_, err := exec.Command("/nonexistent/ffprobe").Output()
	if got := classifyProbe(ctx, nil, err); got.state != "unknown" {
		t.Errorf("missing binary: %+v", got)
	}
}
