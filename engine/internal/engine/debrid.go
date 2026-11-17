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
	if j, err = e.Store.SetResult(id, res); err != nil {
		e.Store.Delete(id)
		return store.Job{}, err
	}
	e.rebuildTree()
	return j, nil
}

// callbackLinks asks cli_debrid for download links and caches them.
type callbackLinks struct {
	url, token string
	client     *http.Client
	mu         sync.Mutex
	cache      map[string]cachedLink
	log        *slog.Logger
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
		cache: map[string]cachedLink{}, log: log}
}

func (c *callbackLinks) Link(ctx context.Context, ref string, refresh bool) (string, error) {
	c.mu.Lock()
	l, ok := c.cache[ref]
	c.mu.Unlock()
	if ok && !refresh && time.Now().Before(l.expires) {
		return l.url, nil
	}
	if c.url == "" {
		return "", errors.New("no link callback configured")
	}
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
			c.log.Warn("debrid link request failed", "ref", ref, "refresh", refresh, "err", err)
		}
		return "", fmt.Errorf("cli_debrid unreachable: %w", err)
	}
	defer resp.Body.Close()
	var out struct {
		URL       string `json:"url"`
		ExpiresIn int    `json:"expires_in"`
		Error     string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil || resp.StatusCode != http.StatusOK || out.URL == "" {
		if out.Error == "" {
			out.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		c.log.Warn("no debrid link", "ref", ref, "refresh", refresh, "status", resp.StatusCode, "err", out.Error)
		return "", fmt.Errorf("no link: %s", out.Error)
	}
	c.log.Debug("debrid link", "ref", ref, "refresh", refresh, "took", time.Since(start).Round(time.Millisecond))
	ttl := time.Duration(out.ExpiresIn) * time.Second
	if ttl <= 0 {
		ttl = 6 * time.Hour
	}
	c.mu.Lock()
	c.cache[ref] = cachedLink{url: out.URL, expires: time.Now().Add(ttl)}
	c.mu.Unlock()
	return out.URL, nil
}
