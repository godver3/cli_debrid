// Package usage keeps how many bytes each usenet server and debrid provider
// delivered per day, so the Mount page can show today, the last 30 days and
// the daily average (like cli_mount's provider usage). It survives engine
// restarts: days are saved to a small JSON file.
package usage

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Days is how far back usage is kept and summed.
const Days = 30

const dateFmt = "2006-01-02"

// Summary is one provider's usage.
type Summary struct {
	TodayBytes  int64 `json:"today_bytes"`
	Last30Bytes int64 `json:"last_30d_bytes"`
	// AvgDailyBytes divides by the days tracked so far (at most 30), so a
	// fresh install isn't averaged over days it never ran.
	AvgDailyBytes int64  `json:"avg_daily_bytes"`
	DaysTracked   int    `json:"days_tracked"`
	Since         string `json:"since,omitempty"` // first tracked day
}

// Tracker turns cumulative byte counters into per-day totals.
type Tracker struct {
	mu    sync.Mutex
	path  string
	days  map[string]map[string]int64 // key -> date -> bytes
	last  map[string]int64            // key -> counter value last seen
	dirty bool
	now   func() time.Time
}

type fileFormat struct {
	Days map[string]map[string]int64 `json:"days"`
}

// Open loads path (missing or unreadable starts empty). An empty path keeps
// everything in memory.
func Open(path string) *Tracker {
	t := &Tracker{path: path, days: map[string]map[string]int64{}, last: map[string]int64{}, now: time.Now}
	if path != "" {
		if b, err := os.ReadFile(path); err == nil {
			var f fileFormat
			if json.Unmarshal(b, &f) == nil && f.Days != nil {
				t.days = f.Days
			}
		}
	}
	t.mu.Lock()
	t.prune()
	t.mu.Unlock()
	return t
}

// Observe records a counter that only grows while the process runs (bytes
// since start). The growth since the last call is added to today. A counter
// that went down was reset, so its new value counts in full.
func (t *Tracker) Observe(key string, cumulative int64) {
	if key == "" || cumulative < 0 {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	prev, seen := t.last[key]
	t.last[key] = cumulative
	delta := cumulative - prev
	if seen && delta < 0 {
		delta = cumulative
	}
	if delta <= 0 {
		return
	}
	t.add(key, delta)
}

// Baseline sets the counter value already accounted for, without counting
// it (counters that outlive the tracker, e.g. process-wide ones).
func (t *Tracker) Baseline(key string, cumulative int64) {
	t.mu.Lock()
	t.last[key] = cumulative
	t.mu.Unlock()
}

// Add counts n bytes for key today.
func (t *Tracker) Add(key string, n int64) {
	if key == "" || n <= 0 {
		return
	}
	t.mu.Lock()
	t.add(key, n)
	t.mu.Unlock()
}

func (t *Tracker) add(key string, n int64) {
	d := t.days[key]
	if d == nil {
		d = map[string]int64{}
		t.days[key] = d
	}
	d[t.now().Format(dateFmt)] += n
	t.dirty = true
}

// prune drops days older than the window. Callers hold mu.
func (t *Tracker) prune() {
	cutoff := t.now().AddDate(0, 0, -(Days - 1)).Format(dateFmt)
	for key, d := range t.days {
		for date := range d {
			if date < cutoff {
				delete(d, date)
				t.dirty = true
			}
		}
		if len(d) == 0 {
			delete(t.days, key)
		}
	}
}

// Summaries returns every key's usage over the last 30 days (today included).
func (t *Tracker) Summaries() map[string]Summary {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.prune()
	now := t.now()
	today := now.Format(dateFmt)
	out := make(map[string]Summary, len(t.days))
	for key, d := range t.days {
		s := Summary{TodayBytes: d[today]}
		for date, n := range d {
			s.Last30Bytes += n
			if s.Since == "" || date < s.Since {
				s.Since = date
			}
		}
		s.DaysTracked = Days
		if first, err := time.ParseInLocation(dateFmt, s.Since, now.Location()); err == nil {
			y, m, dd := now.Date()
			midnight := time.Date(y, m, dd, 0, 0, 0, 0, now.Location())
			s.DaysTracked = min(Days, int(midnight.Sub(first).Hours()/24+0.5)+1)
		}
		if s.DaysTracked > 0 {
			s.AvgDailyBytes = s.Last30Bytes / int64(s.DaysTracked)
		}
		out[key] = s
	}
	return out
}

// Flush writes the file if anything changed.
func (t *Tracker) Flush() error {
	t.mu.Lock()
	if !t.dirty || t.path == "" {
		t.mu.Unlock()
		return nil
	}
	t.prune()
	b, err := json.Marshal(fileFormat{Days: t.days})
	t.dirty = false
	t.mu.Unlock()
	if err == nil {
		err = writeAtomic(t.path, b)
	}
	if err != nil {
		t.mu.Lock()
		t.dirty = true
		t.mu.Unlock()
	}
	return err
}

func writeAtomic(path string, b []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
