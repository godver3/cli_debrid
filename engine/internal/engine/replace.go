package engine

import (
	"bytes"
	"context"
	"errors"
	"io"
	"path"
	"strings"

	"github.com/godver3/cli_debrid/engine/internal/health"
	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nzb"
	"github.com/godver3/cli_debrid/engine/internal/store"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// Playback-repair replacement calls, with the same request/response shapes
// cli_mount uses (database/nzb_playback_repair.py in cli_debrid).

// ReplacementError carries a cli_mount-compatible error code.
type ReplacementError struct{ Code, Message string }

func (e *ReplacementError) Error() string { return e.Message }

// VerifyResult answers "is the replacement playable?".
type VerifyResult struct {
	Status    string `json:"status"` // healthy, broken, unknown
	Reason    string `json:"reason,omitempty"`
	EntryName string `json:"entry_name,omitempty"`
	FileName  string `json:"file_name,omitempty"`
}

var mediaExt = map[string]bool{".mkv": true, ".mp4": true, ".avi": true, ".m4v": true, ".ts": true,
	".m2ts": true, ".mov": true, ".wmv": true, ".webm": true}

func isMedia(name string) bool { return mediaExt[strings.ToLower(path.Ext(name))] }

// VerifyReplacement checks the file of job jobID registered for cliID (or
// its only media file): a full article check, then reads of the first and
// last MiB through the validating reader.
func (e *Engine) VerifyReplacement(ctx context.Context, cliID int, jobID string) (VerifyResult, error) {
	if cliID <= 0 || jobID == "" {
		return VerifyResult{}, &ReplacementError{"invalid_request", "cli_debrid_id and info_hash are required"}
	}
	j, res, err := e.Store.Get(jobID)
	if err != nil {
		return VerifyResult{Status: "unknown", Reason: "replacement_not_ready"}, nil
	}
	switch j.Status {
	case store.Failed:
		if j.Retryable {
			return VerifyResult{Status: "unknown", Reason: "import_inconclusive"}, nil
		}
		return VerifyResult{Status: "broken", Reason: "import_failed", EntryName: e.folderName(jobID)}, nil
	case store.Ready:
	default:
		return VerifyResult{Status: "unknown", Reason: "replacement_not_ready"}, nil
	}
	f, ok := e.folderOf(jobID)
	if !ok {
		return VerifyResult{Status: "unknown", Reason: "replacement_not_ready"}, nil
	}
	entry, ok := pickEntry(f.Entries, j.CLIIDs, cliID)
	if !ok {
		return VerifyResult{Status: "unknown", Reason: "replacement_not_ready"}, nil
	}
	out := VerifyResult{EntryName: f.Name, FileName: entry.Name}
	if !isMedia(entry.Name) {
		return VerifyResult{}, &ReplacementError{"unsupported_media", "exact NZB media is required"}
	}

	data, err := e.Store.NZB(jobID)
	if err != nil {
		return VerifyResult{}, err
	}
	parsed, err := nzb.Parse(bytes.NewReader(data))
	if err != nil {
		return VerifyResult{}, err
	}
	one := &job.Result{Sources: res.Sources, Entries: []job.Entry{entry}}
	rep, err := health.Check(ctx, e.Client, parsed, one, health.Options{Full: true})
	if err != nil {
		return VerifyResult{}, err
	}
	switch rep.Status {
	case health.Broken:
		out.Status, out.Reason = "broken", "usenet_segment_missing"
		return out, nil
	case health.Unknown:
		out.Status, out.Reason = "unknown", "provider_check_inconclusive"
		return out, nil
	}
	files, err := e.Files(jobID)
	if err != nil {
		return VerifyResult{}, err
	}
	r := files.Entry(entry)
	for _, off := range []int64{0, max(0, entry.Size-1<<20)} {
		buf := make([]byte, min(int64(1<<20), entry.Size))
		if _, err := r.ReadAtCtx(ctx, nntp.Live, buf, off); err != nil && !errors.Is(err, io.EOF) {
			if nntp.IsNotFound(err) || errors.Is(err, stream.ErrLayout) {
				out.Status, out.Reason = "broken", "usenet_segment_missing"
				return out, nil
			}
			out.Status, out.Reason = "unknown", "read_inconclusive"
			return out, nil
		}
	}
	out.Status = "healthy"
	return out, nil
}

// pickEntry finds the entry registered for cliID, else the only media
// entry of the job.
func pickEntry(entries []job.Entry, ids map[string]int, cliID int) (job.Entry, bool) {
	for _, en := range entries {
		if ids[en.Name] == cliID {
			return en, true
		}
	}
	var media []job.Entry
	for _, en := range entries {
		if isMedia(en.Name) {
			media = append(media, en)
		}
	}
	if len(media) == 1 {
		return media[0], true
	}
	return job.Entry{}, false
}

// AckRequest identifies an old broken file whose item has been replaced.
type AckRequest struct {
	EntryName   string `json:"entry_name"`
	FileName    string `json:"file_name"`
	InfoHash    string `json:"info_hash"`
	CliDebridID int    `json:"cli_debrid_id"`
	Reason      string `json:"reason"`
}

// AckResult is the cleanup outcome.
type AckResult struct {
	Status       string `json:"status"` // removed, already_removed
	EntryDeleted bool   `json:"entry_deleted"`
}

// AckReplacement removes exactly one old file: it is hidden from the
// mount and its broken record cleared; the job is deleted only when no
// visible file is left (other episodes of a pack stay).
func (e *Engine) AckReplacement(req AckRequest) (AckResult, error) {
	req.EntryName, req.FileName, req.InfoHash = strings.TrimSpace(req.EntryName), strings.TrimSpace(req.FileName), strings.TrimSpace(req.InfoHash)
	if req.FileName == "" || req.InfoHash == "" || req.CliDebridID <= 0 {
		return AckResult{}, &ReplacementError{"invalid_request", "exact replacement identifiers are required"}
	}
	j, res, err := e.Store.Get(req.InfoHash)
	if err != nil {
		return AckResult{Status: "already_removed", EntryDeleted: true}, nil
	}
	if res == nil || contains(j.Hidden, req.FileName) {
		return AckResult{Status: "already_removed"}, nil
	}
	known := false
	for _, en := range uniqueEntries(res.Entries) {
		if en.Name == req.FileName {
			known = true
		}
	}
	if !known {
		return AckResult{Status: "already_removed"}, nil
	}
	if id, ok := j.CLIIDs[req.FileName]; ok && id != 0 && id != req.CliDebridID {
		return AckResult{}, &ReplacementError{"stale_target", "mounted registration changed"}
	}
	j, err = e.Store.Update(req.InfoHash, func(j *store.Job) {
		j.Hidden = append(j.Hidden, req.FileName)
		if j.Health != nil {
			keep := j.Health.Broken[:0:0]
			for _, b := range j.Health.Broken {
				if b.Name != req.FileName {
					keep = append(keep, b)
				}
			}
			h := *j.Health
			h.Broken = keep
			if len(keep) == 0 && h.Status == health.Broken {
				h.Status = "unknown"
			}
			j.Health = &h
		}
	})
	if err != nil {
		return AckResult{}, err
	}
	e.rebuildTree()
	if _, ok := e.folderOf(req.InfoHash); !ok {
		if err := e.Delete(req.InfoHash); err != nil && !errors.Is(err, store.ErrNotFound) {
			return AckResult{}, err
		}
		return AckResult{Status: "removed", EntryDeleted: true}, nil
	}
	return AckResult{Status: "removed"}, nil
}

// folderOf returns a job's folder in the tree.
func (e *Engine) folderOf(id string) (*Folder, bool) {
	e.treeMu.RLock()
	defer e.treeMu.RUnlock()
	for _, f := range e.tree[AllFolder] {
		if f.JobID == id {
			return f, true
		}
	}
	return nil, false
}

func (e *Engine) folderName(id string) string {
	if f, ok := e.folderOf(id); ok {
		return f.Name
	}
	return ""
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
