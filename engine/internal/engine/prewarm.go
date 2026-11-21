package engine

import (
	"context"
	"fmt"
	"path"
	"strings"
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

var prewarming sync.Map // "job/file" -> struct{}: one prewarm per file at a time

// Prewarm fetches the first n bytes of a file in the background at
// prefetch priority, into the caches (disk included), so it starts
// instantly later - cli_debrid asks for the next episode while one plays.
// It returns at once; ok is false when the file is unknown.
func (e *Engine) Prewarm(jobID, file string, n int64) error {
	_, res, err := e.Store.Get(jobID)
	if err != nil {
		return err
	}
	if res == nil {
		return ErrNotReady
	}
	var size int64 = -1
	idx := -1
	for i, en := range res.Entries {
		if en.Name == file {
			idx, size = i, en.Size
		}
	}
	if idx < 0 {
		return fmt.Errorf("engine: %s has no file %q", jobID, file)
	}
	key := jobID + "/" + file
	if _, busy := prewarming.LoadOrStore(key, struct{}{}); busy {
		return nil
	}
	n = min(n, size)
	en := res.Entries[idx]
	go func() {
		defer prewarming.Delete(key)
		files, err := e.Files(jobID)
		if err != nil {
			return
		}
		r := files.Entry(en)
		// A generous backstop, not a speed estimate: a stalled provider
		// must not hold connections forever.
		ctx, cancel := context.WithTimeout(e.ctx, 5*time.Minute)
		defer cancel()
		const chunk = 1 << 20
		jobs := make(chan int64)
		var wg sync.WaitGroup
		for w := 0; w < 4; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer func() {
					if p := recover(); p != nil {
						e.log.Error("prewarm crashed", "job", jobID, "file", file, "panic", p)
					}
				}()
				buf := make([]byte, chunk)
				for off := range jobs {
					r.ReadAtCtx(ctx, nntp.Prefetch, buf[:min(chunk, n-off)], off)
				}
			}()
		}
		start := time.Now()
		for off := int64(0); off < n && ctx.Err() == nil; off += chunk {
			select {
			case jobs <- off:
			case <-ctx.Done():
			}
		}
		close(jobs)
		wg.Wait()
		e.log.Info("prewarmed", "job", jobID, "file", file, "bytes", n, "took", time.Since(start).Round(time.Millisecond))
	}()
	return nil
}

// minPlausibleVideo: smaller files (subtitles, samples, NFOs) are never
// taken as a folder's only video.
const minPlausibleVideo = 20 << 20

// FindFile finds a mount file by name alone, for when the path Plex
// reports can't be mapped onto the mount (Plex sees it at another path, or
// through a virtual folder). Each name is tried in order against every job
// folder: a file of that name (case-insensitive) wins; otherwise a folder
// named after the file (without extension) holding exactly one video-sized
// file, as releases are often named on the folder with the file inside
// obfuscated. Ported from cli_mount's findFileByBasename.
func (e *Engine) FindFile(names []string) (jobID, file string, ok bool) {
	folders := e.Folders(AllFolder)
	for _, name := range names {
		want := strings.ToLower(strings.TrimSpace(name))
		if want == "" {
			continue
		}
		stem := strings.TrimSuffix(want, strings.ToLower(path.Ext(want)))
		for _, f := range folders {
			var only string
			videos := 0
			for _, en := range f.Entries {
				if strings.ToLower(path.Base(en.Name)) == want {
					return f.JobID, en.Name, true
				}
				if en.Size >= minPlausibleVideo {
					videos++
					only = en.Name
				}
			}
			if videos == 1 && strings.ToLower(f.Name) == stem {
				return f.JobID, only, true
			}
		}
	}
	return "", "", false
}
