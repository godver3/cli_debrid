package mount

import "time"

// startedAt stands in for folders without a time of their own (the root,
// the top folders). Go's zero time is year 1, before the Windows FILETIME
// epoch (1601): cmd, PowerShell and Explorer reject it ("not a valid Win32
// FileTime"), and Linux shows it as year 1.
var startedAt = time.Now()

func stamp(t time.Time) time.Time {
	if t.IsZero() {
		return startedAt
	}
	return t
}
