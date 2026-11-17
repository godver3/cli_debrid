package mount

import (
	"bufio"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// fsType is how our mounts show in /proc/self/mountinfo (go-fuse prefixes
// the Name option with "fuse.").
const fsType = "fuse.clid"

// ourMounts counts clid-engine mounts stacked on path.
func ourMounts(path string) int {
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return 0
	}
	defer f.Close()
	want := filepath.Clean(path)
	n := 0
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		// id parent maj:min root mountpoint opts [optional...] - fstype source superopts
		fields := strings.Fields(sc.Text())
		sep := -1
		for i, x := range fields {
			if x == "-" {
				sep = i
				break
			}
		}
		if sep < 0 || len(fields) < 5 || sep+1 >= len(fields) {
			continue
		}
		mp := strings.NewReplacer(`\040`, " ", `\011`, "\t", `\134`, `\`).Replace(fields[4])
		if filepath.Clean(mp) == want && fields[sep+1] == fsType {
			n++
		}
	}
	return n
}

func fusermount() string {
	for _, name := range []string{"fusermount3", "fusermount"} {
		if p, err := exec.LookPath(name); err == nil {
			return p
		}
	}
	return "fusermount3"
}

// LazyUnmount detaches path even while files are open (fusermount -uz);
// readers still holding files finish against the detached mount.
func LazyUnmount(path string) error {
	out, err := exec.Command(fusermount(), "-uz", path).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// ClearStale removes clid-engine mounts left on path by an engine that
// exited without unmounting (crash, kill, busy unmount). Without this a
// new mount stacks on top of a dead one. Other filesystems are never
// touched.
func ClearStale(path string, log *slog.Logger) {
	for i := 0; i < 10 && ourMounts(path) > 0; i++ {
		if err := LazyUnmount(path); err != nil {
			log.Warn("could not clear old mount", "path", path, "err", err)
			return
		}
		log.Info("cleared old mount", "path", path)
	}
}
