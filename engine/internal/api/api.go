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
//	GET    /health?status=broken   jobs with health records (filtered)
//	POST   /health/run?full=&ignore_last_checked=   check all ready jobs
//	GET    /health/run             current/last run; POST /health/stop stops it
//	GET|POST|DELETE /jobs/{id}/health   record / check now (?full=) / clear
//	POST   /replacements/verify    {cli_debrid_id, info_hash}: is the replacement playable?
//	POST   /replacements/ack       {entry_name, file_name, info_hash, cli_debrid_id, reason}:
//	                               remove one old broken file (cli_mount-compatible shapes)
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
	mux.HandleFunc("GET /streams", a.streams)
	mux.HandleFunc("POST /prewarm", a.prewarm)
	mux.HandleFunc("GET /health", a.healthList)
	mux.HandleFunc("POST /health/run", a.healthRun)
	mux.HandleFunc("GET /health/run", a.healthRunStatus)
	mux.HandleFunc("POST /health/stop", a.healthStop)
	mux.HandleFunc("GET /jobs/{id}/health", a.healthGet)
	mux.HandleFunc("POST /jobs/{id}/health", a.healthCheck)
	mux.HandleFunc("DELETE /jobs/{id}/health", a.healthClear)
	mux.HandleFunc("POST /replacements/verify", a.verify)
	mux.HandleFunc("POST /replacements/ack", a.ack)
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

// prewarm starts fetching the start of one file in the background
// (cli_debrid's next-episode prewarm). Body: {"job_id","file","bytes"}.
func (a *api) prewarm(w http.ResponseWriter, r *http.Request) {
	var body struct {
		JobID string `json:"job_id"`
		File  string `json:"file"`
		Bytes int64  `json:"bytes"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body); err != nil || body.JobID == "" || body.File == "" || body.Bytes <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "job_id, file and bytes are required"})
		return
	}
	if err := a.e.Prewarm(body.JobID, body.File, body.Bytes); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "prewarming"})
}

// streams lists files open on the mount (for "now playing").
func (a *api) streams(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.e.Streams())
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

func truthy(s string) bool {
	return s == "1" || strings.EqualFold(s, "true") || strings.EqualFold(s, "yes")
}

func (a *api) healthList(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	out := []jobOut{}
	for _, j := range a.e.Store.List() {
		if j.Health == nil || (status != "" && j.Health.Status != status) {
			continue
		}
		out = append(out, a.render(j, false))
	}
	writeJSON(w, http.StatusOK, out)
}

func (a *api) healthRun(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	writeJSON(w, http.StatusAccepted, a.e.StartHealthRun(truthy(q.Get("full")), truthy(q.Get("ignore_last_checked"))))
}

func (a *api) healthRunStatus(w http.ResponseWriter, r *http.Request) {
	run := a.e.HealthRunStatus()
	if run == nil {
		writeJSON(w, http.StatusOK, map[string]any{"status": "idle"})
		return
	}
	writeJSON(w, http.StatusOK, run)
}

func (a *api) healthStop(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"stopped": a.e.StopHealthRun()})
}

func (a *api) healthGet(w http.ResponseWriter, r *http.Request) {
	j, _, err := a.e.Store.Get(r.PathValue("id"))
	if err != nil {
		storeErr(w, err)
		return
	}
	if j.Health == nil {
		writeJSON(w, http.StatusOK, store.Health{Status: "unknown"})
		return
	}
	writeJSON(w, http.StatusOK, j.Health)
}

func (a *api) healthCheck(w http.ResponseWriter, r *http.Request) {
	err := a.e.CheckHealth(r.PathValue("id"), truthy(r.URL.Query().Get("full")))
	switch {
	case err == nil:
		writeJSON(w, http.StatusAccepted, map[string]string{"status": "checking"})
	case errors.Is(err, engine.ErrNotReady):
		writeErr(w, http.StatusConflict, err)
	default:
		storeErr(w, err)
	}
}

func (a *api) healthClear(w http.ResponseWriter, r *http.Request) {
	if err := a.e.ClearHealth(r.PathValue("id")); err != nil {
		storeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func replacementErr(w http.ResponseWriter, err error) {
	var re *engine.ReplacementError
	if errors.As(err, &re) {
		code := http.StatusConflict
		if re.Code == "invalid_request" || re.Code == "unsupported_media" {
			code = http.StatusBadRequest
		}
		writeJSON(w, code, map[string]string{"code": re.Code, "message": re.Message})
		return
	}
	writeJSON(w, http.StatusInternalServerError, map[string]string{"code": "failed", "message": err.Error()})
}

func (a *api) verify(w http.ResponseWriter, r *http.Request) {
	var req struct {
		CliDebridID int    `json:"cli_debrid_id"`
		InfoHash    string `json:"info_hash"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "invalid_request", "message": "invalid JSON body"})
		return
	}
	res, err := a.e.VerifyReplacement(r.Context(), req.CliDebridID, req.InfoHash)
	if err != nil {
		replacementErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (a *api) ack(w http.ResponseWriter, r *http.Request) {
	var req engine.AckRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "invalid_request", "message": "invalid JSON body"})
		return
	}
	res, err := a.e.AckReplacement(req)
	if err != nil {
		replacementErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}
