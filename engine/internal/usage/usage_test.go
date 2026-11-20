package usage

import (
	"path/filepath"
	"testing"
	"time"
)

func at(t *Tracker, s string) {
	ts, _ := time.ParseInLocation("2006-01-02 15:04", s, time.Local)
	t.now = func() time.Time { return ts }
}

func TestObserveCountsGrowthAndResets(t *testing.T) {
	tr := Open("")
	at(tr, "2026-11-20 10:00")
	tr.Observe("usenet/a", 100) // first sight: process started at 0
	tr.Observe("usenet/a", 250)
	tr.Observe("usenet/a", 50) // counter reset (engine restarted its pools)
	s := tr.Summaries()["usenet/a"]
	if s.TodayBytes != 300 || s.Last30Bytes != 300 || s.DaysTracked != 1 || s.AvgDailyBytes != 300 {
		t.Fatalf("got %+v", s)
	}
}

func TestWindowAverageAndPersistence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "usage.json")
	tr := Open(path)
	at(tr, "2026-10-01 12:00")
	tr.Add("debrid/rd", 1000) // falls out of the window below
	at(tr, "2026-11-11 12:00")
	tr.Add("debrid/rd", 600)
	at(tr, "2026-11-20 23:30")
	tr.Add("debrid/rd", 400)
	if err := tr.Flush(); err != nil {
		t.Fatal(err)
	}
	re := Open(path)
	at(re, "2026-11-20 23:45")
	s := re.Summaries()["debrid/rd"]
	if s.TodayBytes != 400 || s.Last30Bytes != 1000 || s.Since != "2026-11-11" || s.DaysTracked != 10 || s.AvgDailyBytes != 100 {
		t.Fatalf("got %+v", s)
	}
	at(re, "2026-12-20 00:10")
	if _, ok := re.Summaries()["debrid/rd"]; ok {
		t.Fatal("days older than 30 should be gone")
	}
}
