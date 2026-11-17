// Package api is the engine's localhost HTTP API, used by cli_debrid.
//
//	GET    /status                 engine, providers, caches, job counts
//	POST   /jobs?name=&category=&id=   body: NZB (raw, or multipart field "nzb")
//	GET    /jobs?status=           list
//	GET    /jobs/{id}              job + entries (with mount paths) + failures
//	PATCH  /jobs/{id}              {"name": ..., "tags": [...], "cli_ids": {...}}
//	DELETE /jobs/{id}
//	POST   /jobs/{id}/retry        re-queue a failed job
//	GET    /changes?since=<unix ms>  jobs updated and deleted since
//	GET    /stats                  connection pools and transfer counters
package api

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/engine"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

const maxNZB = 64 << 20

// Handler returns the API handler for e.
func Handler(e *engine.Engine) http.Handler {
	mux := http.NewServeMux()
	a := &api{e: e}
	mux.HandleFunc("GET /status", a.status)
	mux.HandleFunc("POST /jobs", a.submit)
	mux.HandleFunc("GET /jobs", a.list)
	mux.HandleFunc("GET /jobs/{id}", a.get)
	mux.HandleFunc("PATCH /jobs/{id}", a.patch)
	mux.HandleFunc("DELETE /jobs/{id}", a.del)
	mux.HandleFunc("POST /jobs/{id}/retry", a.retry)
	mux.HandleFunc("GET /changes", a.changes)
	mux.HandleFunc("GET /stats", a.stats)
	return a.auth(mux)
}

type api struct{ e *engine.Engine }

func (a *api) auth(next http.Handler) http.Handler {
	token := a.e.Config().Token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
				writeErr(w, http.StatusUnauthorized, errors.New("bad or missing token"))
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

func storeErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeErr(w, http.StatusNotFound, err)
	case errors.Is(err, store.ErrExists):
		writeErr(w, http.StatusConflict, err)
	case errors.Is(err, store.ErrBadID), errors.Is(err, engine.ErrBadNZB):
		writeErr(w, http.StatusBadRequest, err)
	default:
		writeErr(w, http.StatusInternalServerError, err)
	}
}

type entryOut struct {
	Name        string `json:"name"`
	Size        int64  `json:"size"`
	Path        string `json:"path,omitempty"`
	FromArchive string `json:"from_archive,omitempty"`
}

type jobOut struct {
	store.Job
	Path     string     `json:"path,omitempty"` // job folder in the mount
	Entries  []entryOut `json:"entries,omitempty"`
	Failures any        `json:"failures,omitempty"`
}

func (a *api) render(j store.Job, detail bool) jobOut {
	out := jobOut{Job: j, Path: a.e.FolderPath(j.ID)}
	if !detail {
		return out
	}
	if _, res, err := a.e.Store.Get(j.ID); err == nil && res != nil {
		if len(res.Failures) > 0 {
			out.Failures = res.Failures
		}
	}
	if out.Path != "" {
		name := out.Path[strings.LastIndex(out.Path, "/")+1:]
		if f, ok := a.e.Folder(engine.AllFolder, name); ok {
			for _, en := range f.Entries {
				out.Entries = append(out.Entries, entryOut{Name: en.Name, Size: en.Size,
					Path: out.Path + "/" + en.Name, FromArchive: en.FromArchive})
			}
		}
	}
	return out
}

func (a *api) status(w http.ResponseWriter, r *http.Request) {
	counts := map[string]int{}
	for _, j := range a.e.Store.List() {
		counts[j.Status]++
	}
	pools, _, _, _ := a.e.Client.Stats()
	out := map[string]any{
		"ready":      true,
		"mount_path": a.e.Config().MountPath,
		"jobs":       counts,
		"providers":  pools,
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *api) submit(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxNZB)
	var data []byte
	var err error
	if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data") {
		f, _, ferr := r.FormFile("nzb")
		if ferr != nil {
			writeErr(w, http.StatusBadRequest, ferr)
			return
		}
		defer f.Close()
		data, err = io.ReadAll(f)
	} else {
		data, err = io.ReadAll(r.Body)
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	q := r.URL.Query()
	j, err := a.e.Submit(data, q.Get("name"), q.Get("category"), q.Get("id"))
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, a.render(j, false))
}

func (a *api) list(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	out := []jobOut{}
	for _, j := range a.e.Store.List() {
		if status == "" || j.Status == status {
			out = append(out, a.render(j, false))
		}
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *api) get(w http.ResponseWriter, r *http.Request) {
	j, _, err := a.e.Store.Get(r.PathValue("id"))
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, a.render(j, true))
}

func (a *api) patch(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		Name   *string         `json:"name"`
		Tags   *[]string       `json:"tags"`
		CLIIDs *map[string]int `json:"cli_ids"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if body.Name != nil {
		if _, err := a.e.Rename(id, *body.Name); err != nil {
			storeErr(w, err)
			return
		}
	}
	j, err := a.e.Store.Update(id, func(j *store.Job) {
		if body.Tags != nil {
			j.Tags = *body.Tags
		}
		if body.CLIIDs != nil {
			if j.CLIIDs == nil {
				j.CLIIDs = map[string]int{}
			}
			for k, v := range *body.CLIIDs { // merge, like cli_mount
				j.CLIIDs[k] = v
			}
		}
	})
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, a.render(j, true))
}

func (a *api) del(w http.ResponseWriter, r *http.Request) {
	if err := a.e.Delete(r.PathValue("id")); err != nil {
		storeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (a *api) retry(w http.ResponseWriter, r *http.Request) {
	j, err := a.e.Retry(r.PathValue("id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			storeErr(w, err)
		} else {
			writeErr(w, http.StatusConflict, err)
		}
		return
	}
	writeJSON(w, http.StatusOK, a.render(j, false))
}

func (a *api) changes(w http.ResponseWriter, r *http.Request) {
	var since time.Time
	if s := r.URL.Query().Get("since"); s != "" {
		ms, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			writeErr(w, http.StatusBadRequest, errors.New("since must be unix milliseconds"))
			return
		}
		since = time.UnixMilli(ms)
	}
	now := time.Now()
	jobs, gone := a.e.Store.Changes(since)
	out := make([]jobOut, 0, len(jobs))
	for _, j := range jobs {
		out = append(out, a.render(j, true))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"now":     now.UnixMilli(), // pass back as since next time
		"jobs":    out,
		"deleted": gone,
	})
}

func (a *api) stats(w http.ResponseWriter, r *http.Request) {
	pools, bytes, articles, missing := a.e.Client.Stats()
	segs, memBytes := a.e.Cache.Len()
	out := map[string]any{
		"pools":            pools,
		"bytes_downloaded": bytes,
		"articles":         articles,
		"articles_missing": missing,
		"mem_cache":        map[string]int64{"segments": int64(segs), "bytes": memBytes},
	}
	if a.e.Disk != nil {
		files, used := a.e.Disk.Usage()
		out["disk_cache"] = map[string]int64{"segments": int64(files), "bytes": used,
			"hits": a.e.Disk.Hits.Load(), "misses": a.e.Disk.Misses.Load()}
	}
	writeJSON(w, http.StatusOK, out)
}
