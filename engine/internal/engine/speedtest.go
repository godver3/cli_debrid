package engine

import (
	"bytes"
	"context"
	"errors"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// Speed test limits: enough data for a steady rate, short enough to click.
const (
	speedTestBytes   = 64 << 20
	speedTestTime    = 20 * time.Second
	speedTestConns   = 8
	speedTestJobs    = 8
	speedTestArticle = 64
)

// SpeedTest measures one usenet server with real articles from the newest
// ready jobs (newest = most likely still on the server). It reads straight
// from the server, never from the cache, and never takes the playback
// reserve.
func (e *Engine) SpeedTest(ctx context.Context, provider string) (nntp.SpeedResult, error) {
	ids := e.speedTestArticles()
	if len(ids) == 0 {
		return nntp.SpeedResult{Provider: provider}, errors.New("no usenet jobs to test with yet: import an NZB first")
	}
	ctx, cancel := context.WithTimeout(ctx, speedTestTime)
	defer cancel()
	res, err := e.Client.SpeedTest(ctx, provider, ids, speedTestConns, speedTestBytes)
	if err == nil {
		e.log.Info("speed test", "provider", provider, "mb_per_second", round1(res.MBps),
			"latency_ms", round1(res.LatencyMs), "articles", res.Articles, "missing", res.Missing, "err", res.Error)
	}
	return res, err
}

func round1(f float64) float64 { return float64(int64(f*10+0.5)) / 10 }

// speedTestArticles picks articles spread through the largest file of the
// newest ready usenet jobs.
func (e *Engine) speedTestArticles() []string {
	var ids []string
	jobs := 0
	for _, j := range e.Store.List() {
		if jobs >= speedTestJobs || len(ids) >= speedTestArticle {
			break
		}
		if j.Status != store.Ready || j.Protocol == ProtocolDebrid {
			continue
		}
		data, err := e.Store.NZB(j.ID)
		if err != nil {
			continue
		}
		n, err := nzb.Parse(bytes.NewReader(data))
		if err != nil || len(n.Files) == 0 {
			continue
		}
		big := n.Files[0]
		for _, f := range n.Files[1:] {
			if len(f.Segments) > len(big.Segments) {
				big = f
			}
		}
		if len(big.Segments) == 0 {
			continue
		}
		jobs++
		per := speedTestArticle / speedTestJobs
		step := max(1, len(big.Segments)/(per+1))
		for i := step; i < len(big.Segments) && per > 0; i += step {
			ids = append(ids, big.Segments[i].ID)
			per--
		}
	}
	return ids
}
