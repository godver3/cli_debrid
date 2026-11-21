package engine

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Debrid files are streamed over HTTP. cli_debrid owns the provider
// accounts: it registers the account's torrents (POST /debrid/jobs) and
// turns a file reference into a download link when the engine asks
// (LinkCallback), so every provider cli_debrid supports works here.

// ProtocolDebrid marks debrid jobs.
const ProtocolDebrid = "debrid"

// ErrNotUsenet: usenet-only operations (health checks) on a debrid job.
var ErrNotUsenet = errors.New("engine: not a usenet job")

// DebridFile is one file of a debrid torrent.
type DebridFile struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	Ref  string `json:"ref"` // provider file reference, handed back to the link callback
}

// AddDebrid registers a debrid torrent as a ready job (no download).
func (e *Engine) AddDebrid(id, name, category, provider string, files []DebridFile) (store.Job, error) {
	if !store.ValidID(id) || strings.TrimSpace(name) == "" || len(files) == 0 {
		return store.Job{}, fmt.Errorf("%w: valid id, name and files are required", ErrBadNZB)
	}
	res := &job.Result{}
	seen := map[string]bool{}
	for i, f := range files {
		if f.Name == "" || f.Size <= 0 || f.Ref == "" || seen[f.Name] || strings.ContainsAny(f.Name, "/\\") {
			return store.Job{}, fmt.Errorf("%w: file %d (%q) needs a unique plain name, size and ref", ErrBadNZB, i, f.Name)
		}
		seen[f.Name] = true
		res.Sources = append(res.Sources, job.Source{NZBIndex: -1, Name: f.Name, Layout: stream.Layout{Size: f.Size}, Remote: f.Ref})
		res.Entries = append(res.Entries, job.Entry{Name: f.Name, Size: f.Size, Pieces: []job.Piece{{Source: i, Length: f.Size}}})
	}
	j, err := e.Store.Create(store.Job{ID: id, Name: cleanFolderName(name), Category: category, Protocol: ProtocolDebrid, Provider: provider},
		[]byte("<nzb></nzb>"))
	if err != nil {
		return store.Job{}, err
	}
	if j, err = e.publish(id, res); err != nil {
		e.Store.Delete(id)
		return store.Job{}, err
	}
	e.log.Info("debrid job received", "job", id, "name", j.Name, "provider", provider, "files", len(files))
	return j, nil
}

// callbackLinks asks cli_debrid for download links and caches them.
type callbackLinks struct {
	url, token string
	client     *http.Client
	mu         sync.Mutex
	cache      map[string]cachedLink
	pending    map[string]*linkCall
	names      func(ref string) string
	opts       stream.RemoteOptions
	log        *slog.Logger
}

// RemoteOptions: the debrid streaming settings every file gets.
func (c *callbackLinks) RemoteOptions() stream.RemoteOptions { return c.opts }

// linkCall is one callback in flight; readers missing the cache for the same
// ref at the same time wait for it instead of each asking cli_debrid (which
// would mint a new link at the provider per reader).
type linkCall struct {
	done chan struct{}
	url  string
	err  error
}

type cachedLink struct {
	url     string
	expires time.Time
}

func newCallbackLinks(url, token string, log *slog.Logger) *callbackLinks {
	if log == nil {
		log = slog.Default()
	}
	return &callbackLinks{url: url, token: token, client: &http.Client{Timeout: 30 * time.Second},
		cache: map[string]cachedLink{}, pending: map[string]*linkCall{}, log: log}
}

// name is the "job/file" a ref belongs to, for log lines ("" when unknown).
func (c *callbackLinks) name(ref string) string {
	if c.names == nil {
		return ""
	}
	return c.names(ref)
}

func (c *callbackLinks) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	key := ref
	if refresh {
		key += "\x00refresh"
	}
	c.mu.Lock()
	l, ok := c.cache[ref]
	if ok && !refresh && time.Now().Before(l.expires) {
		c.mu.Unlock()
		return l.url, nil
	}
	if c.url == "" {
		c.mu.Unlock()
		return "", errors.New("no link callback configured")
	}
	if call, ok := c.pending[key]; ok {
		c.mu.Unlock()
		select {
		case <-call.done:
			return call.url, call.err
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	call := &linkCall{done: make(chan struct{})}
	c.pending[key] = call
	c.mu.Unlock()
	// The waiters must not fail because the first reader went away.
	call.url, call.err = c.fetch(context.WithoutCancel(ctx), ref, refresh)
	c.mu.Lock()
	delete(c.pending, key)
	c.mu.Unlock()
	close(call.done)
	return call.url, call.err
}

func (c *callbackLinks) fetch(ctx context.Context, ref string, refresh bool) (string, error) {
	body, _ := json.Marshal(map[string]any{"ref": ref, "refresh": refresh})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.token)
	start := time.Now()
	resp, err := c.client.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			c.log.Warn("debrid link request failed", "file", c.name(ref), "ref", ref, "refresh", refresh, "err", err)
		}
		return "", fmt.Errorf("cli_debrid unreachable: %w", err)
	}
	defer resp.Body.Close()
	var out struct {
		URL       string `json:"url"`
		ExpiresIn int    `json:"expires_in"`
		Error     string `json:"error"`
		Dead      bool   `json:"dead"`
		Reason    string `json:"reason"`
	}
	decErr := json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode == http.StatusGone && decErr == nil && out.Dead && out.Reason != "" {
		c.log.Warn("debrid file gone", "file", c.name(ref), "ref", ref, "reason", out.Reason, "err", out.Error)
		return "", &stream.DeadFileError{Reason: out.Reason, Detail: out.Error}
	}
	if decErr != nil || resp.StatusCode != http.StatusOK || out.URL == "" {
		if out.Error == "" {
			out.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		c.log.Warn("no debrid link", "file", c.name(ref), "ref", ref, "refresh", refresh, "status", resp.StatusCode, "err", out.Error)
		return "", fmt.Errorf("no link: %s", out.Error)
	}
	c.log.Debug("debrid link", "file", c.name(ref), "ref", ref, "refresh", refresh, "expires_in", out.ExpiresIn,
		"took", time.Since(start).Round(time.Millisecond))
	ttl := time.Duration(out.ExpiresIn) * time.Second
	if ttl <= 0 {
		ttl = 6 * time.Hour
	}
	c.mu.Lock()
	c.cache[ref] = cachedLink{url: out.URL, expires: time.Now().Add(ttl)}
	c.mu.Unlock()
	return out.URL, nil
}

// Check asks cli_debrid whether a debrid file still exists, by the same
// rules cli_mount's repair sweep uses per provider. state is healthy,
// broken or unknown; only broken is proof.
func (c *callbackLinks) Check(ctx context.Context, ref string) (state, reason string, err error) {
	if c.url == "" {
		return "", "", errors.New("no link callback configured")
	}
	checkURL := strings.TrimSuffix(c.url, "/link") + "/check"
	body, _ := json.Marshal(map[string]any{"ref": ref})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, checkURL, bytes.NewReader(body))
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("cli_debrid unreachable: %w", err)
	}
	defer resp.Body.Close()
	var out struct {
		State  string `json:"state"`
		Reason string `json:"reason"`
		Error  string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil || resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("check: HTTP %d %s", resp.StatusCode, out.Error)
	}
	switch out.State {
	case health.Healthy, health.Broken:
		return out.State, out.Reason, nil
	}
	return "unknown", out.Reason, nil
}
