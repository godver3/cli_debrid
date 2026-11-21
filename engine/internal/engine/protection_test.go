package engine

import (
	"testing"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
	"github.com/godver3/cli_debrid/engine/internal/nntptest"
)

func TestReadPriorityFollowsPlexProtection(t *testing.T) {
	s := nntptest.Start(t)
	off := newEngine(t, s, nil)
	off.SetProtected([]ProtectedFile{{JobID: "a", File: "x.mkv"}}, true, "")
	if off.ReadPriority("b", "y.mkv") != nntp.Live {
		t.Fatal("protection off: every mount read is Live")
	}

	e := newEngine(t, s, func(c *Config) { c.PlexProtection = true })
	if e.ReadPriority("b", "y.mkv") != nntp.Live {
		t.Fatal("no poll yet: Live")
	}
	e.SetProtected(nil, true, "")
	if e.ReadPriority("b", "y.mkv") != nntp.Live {
		t.Fatal("nothing playing: the scanner may use the reserve")
	}
	e.SetProtected([]ProtectedFile{{JobID: "a", File: "x.mkv"}}, true, "")
	if e.ReadPriority("b", "y.mkv") != nntp.Live || e.Protection().Active {
		t.Fatal("playing but no import running: nothing to protect from, everything Live")
	}
	e.importsActive.Add(1) // an import is running
	defer e.importsActive.Add(-1)
	if e.ReadPriority("a", "x.mkv") != nntp.Live || e.ReadPriority("b", "y.mkv") != nntp.Read {
		t.Fatal("while Plex plays a/x.mkv during an import only it is Live")
	}
	e.SetProtected([]ProtectedFile{{JobID: "a", File: "x.mkv"}}, false, "plex down")
	if e.ReadPriority("b", "y.mkv") != nntp.Live {
		t.Fatal("failed poll: protection off")
	}
	e.SetProtected([]ProtectedFile{{JobID: "a", File: "x.mkv"}}, true, "")
	e.protect.at = time.Now().Add(-ProtectionTTL - time.Second)
	if e.ReadPriority("b", "y.mkv") != nntp.Live {
		t.Fatal("stale poll: protection off")
	}
	if st := e.Protection(); !st.Enabled || st.Active || len(st.Files) != 1 {
		t.Fatalf("status %+v", st)
	}
}
