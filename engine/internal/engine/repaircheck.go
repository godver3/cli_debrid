package engine

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// The repair checks below follow cli_mount's repair sweep, so the built-in
// mount marks exactly what cli_mount would: proof only, never a timeout.

// headerProbeTimeout is cli_mount's repairMediaProbeTimeout.
const headerProbeTimeout = 60 * time.Second

// headerCheck reads the first bytes of each file the article check didn't
// already prove broken. A wrong container header is proof (cli_mount's
// media_header_invalid); an article proven missing on the way is too. Any
// other read error proves nothing and is skipped.
func (e *Engine) headerCheck(id string, res *job.Result, rep health.Report) []store.BrokenFile {
	broken := map[string]bool{}
	for _, f := range rep.Broken() {
		broken[f.Name] = true
	}
	fs, err := e.Files(id)
	if err != nil {
		return nil
	}
	var out []store.BrokenFile
	now := time.Now()
	for _, en := range res.Entries {
		if broken[en.Name] || en.Size < job.MediaHeaderLen || job.MediaHeaderValid(en.Name, nil) {
			continue // proven already, too small, or a type without a known header
		}
		ctx, cancel := context.WithTimeout(e.ctx, headerProbeTimeout)
		head := make([]byte, job.MediaHeaderLen)
		_, err := fs.Entry(en).ReadAtCtx(ctx, nntp.Read, head, 0)
		cancel()
		switch {
		case err == nil && !job.MediaHeaderValid(en.Name, head):
			out = append(out, store.BrokenFile{Name: en.Name, Reason: ReasonHeaderInvalid,
				Detail: fmt.Sprintf("file starts %x, not a %s header", head, extOf(en.Name)), Via: "check", At: now})
		case err != nil && nntp.IsNotFound(err):
			seg := ""
			var se *stream.SegmentError
			if errors.As(err, &se) {
				seg = se.ID
			}
			out = append(out, store.BrokenFile{Name: en.Name, Reason: ReasonSegmentMissing,
				Detail: "first article missing on every provider", Segment: seg, Via: "check", At: now})
		}
	}
	if len(out) > 0 {
		e.log.Warn("health check: files can't play", "job", id, "files", len(out), "reason", out[0].Reason)
	}
	return out
}

func extOf(name string) string {
	for i := len(name) - 1; i >= 0 && name[i] != '/'; i-- {
		if name[i] == '.' {
			return name[i:]
		}
	}
	return "media"
}

// debridCheckTimeout bounds one file's check (cli_debrid asks the provider).
const debridCheckTimeout = 45 * time.Second

// runDebridCheck asks cli_debrid about every file of a debrid job, by the
// rules cli_mount's sweep applies per provider. broken = the provider says
// the file is gone; anything else that isn't a clear yes is unknown.
func (e *Engine) runDebridCheck(id string) {
	select {
	case e.healthSem <- struct{}{}:
		defer func() { <-e.healthSem }()
	case <-e.ctx.Done():
		return
	}
	j, res, err := e.Store.Get(id)
	if err != nil || res == nil {
		return
	}
	now := time.Now()
	h := store.Health{CheckedAt: now}
	healthy := 0
	for _, en := range res.Entries {
		if contains(j.Hidden, en.Name) || len(en.Pieces) == 0 {
			continue
		}
		src := en.Pieces[0].Source
		if src < 0 || src >= len(res.Sources) || res.Sources[src].Remote == "" {
			continue
		}
		ctx, cancel := context.WithTimeout(e.ctx, debridCheckTimeout)
		state, reason, err := e.links.Check(ctx, res.Sources[src].Remote)
		cancel()
		switch {
		case err != nil:
			h.Inconclusive++
			e.log.Debug("debrid check inconclusive", "job", id, "file", en.Name, "err", err)
		case state == health.Healthy:
			healthy++
		case state == health.Broken:
			if reason == "" {
				reason = ReasonHosterGone
			}
			h.Broken = append(h.Broken, store.BrokenFile{Name: en.Name, Reason: reason, Via: "check", At: now})
		default:
			h.Inconclusive++
		}
	}
	switch {
	case len(h.Broken) > 0:
		h.Status = health.Broken
	case healthy > 0 && h.Inconclusive == 0:
		h.Status = health.Healthy
	default:
		h.Status = health.Unknown
	}
	e.Store.Update(id, func(j *store.Job) {
		// Proof from playback stays unless this check covered the file.
		if j.Health != nil {
			for _, b := range j.Health.Broken {
				if b.Via == "playback" && !hasBroken(h.Broken, b.Name) {
					h.Broken = append(h.Broken, b)
					h.Status = health.Broken
				}
			}
		}
		j.Health = &h
	})
	e.rebuildTree()
	e.log.Info("debrid health checked", "job", id, "status", h.Status, "broken", len(h.Broken), "inconclusive", h.Inconclusive)
}
