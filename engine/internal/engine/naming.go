package engine

import (
	"fmt"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/store"
)

// ---- allowed file extensions ----------------------------------------------

// DefaultAllowedExt: files the mount shows when no list is configured.
var DefaultAllowedExt = []string{"mkv", "mp4", "m4v", "avi", "mov", "mpg", "mpeg"}

// extAllowed: an empty list shows everything.
func extAllowed(allowed []string, name string) bool {
	if len(allowed) == 0 {
		return true
	}
	ext := strings.TrimPrefix(strings.ToLower(path.Ext(name)), ".")
	for _, a := range allowed {
		if strings.TrimPrefix(strings.ToLower(strings.TrimSpace(a)), ".") == ext {
			return true
		}
	}
	return false
}

// ---- deobfuscation (cli_mount's deobfuscate_mode) -------------------------

// Deobfuscate modes: "" off, "index" (<job> - 01.mkv), "season_ep"
// (S01E01.mkv when the job name carries a season, else like index).
const (
	DeobfuscateIndex    = "index"
	DeobfuscateSeasonEp = "season_ep"
)

var (
	reSeason     = regexp.MustCompile(`(?i)[.\s]S(\d{1,2})[.\s]|Season\s(\d{1,2})`)
	reHexSuffix  = regexp.MustCompile(`^(.+)\.([a-f0-9]{8,})$`)
	videoExtList = map[string]bool{"mkv": true, "mp4": true, "m4v": true, "avi": true, "mov": true, "mpg": true,
		"mpeg": true, "ts": true, "m2ts": true, "wmv": true, "webm": true, "vob": true, "iso": true}
)

func isVideo(name string) bool {
	return videoExtList[strings.TrimPrefix(strings.ToLower(path.Ext(name)), ".")]
}

// obfuscatedName: a hash-like base name (no separators, letters and digits,
// 12+ chars) or a "<name>.<hex>" suffix.
func obfuscatedName(name string) bool {
	base := strings.TrimSuffix(name, path.Ext(name))
	if base == "" || reHexSuffix.MatchString(base) {
		return true
	}
	if strings.ContainsAny(base, ".-_ []()") {
		return false
	}
	letter, digit := false, false
	for _, r := range base {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z':
			letter = true
		case r >= '0' && r <= '9':
			digit = true
		default:
			return false
		}
	}
	return len(base) >= 12 && letter && digit
}

// Deobfuscate renames a new import's video files the way cli_mount does:
// a single video file always takes the job name (cli_debrid names jobs);
// several keep their names when those are meaningful and unique, and are
// otherwise numbered by the mode. Returns how many were renamed.
func Deobfuscate(res *job.Result, mode, jobName string) int {
	if res == nil || (mode != DeobfuscateIndex && mode != DeobfuscateSeasonEp) || jobName == "" {
		return 0
	}
	var media []*job.Entry
	for i := range res.Entries {
		if isVideo(res.Entries[i].Name) {
			media = append(media, &res.Entries[i])
		}
	}
	if len(media) == 0 {
		return 0
	}
	if len(media) == 1 {
		ext := path.Ext(media[0].Name)
		name := jobName
		if ext != "" && !strings.EqualFold(path.Ext(jobName), ext) {
			name += ext
		}
		if name == media[0].Name {
			return 0
		}
		media[0].Name = cleanFolderName(name)
		return 1
	}
	unique := map[string]bool{}
	keep := true
	for _, m := range media {
		if obfuscatedName(m.Name) || unique[m.Name] {
			keep = false
			break
		}
		unique[m.Name] = true
	}
	if keep {
		return 0
	}
	// Posting order is the episode order for numbered packs.
	ordered := append([]*job.Entry(nil), media...)
	sortByPieceOrder(res, ordered)
	season := ""
	if m := reSeason.FindStringSubmatch(jobName); m != nil {
		season = m[1] + m[2]
	}
	for i, m := range ordered {
		ext := path.Ext(m.Name)
		if mode == DeobfuscateSeasonEp && season != "" {
			n, _ := strconv.Atoi(season)
			m.Name = fmt.Sprintf("S%02dE%02d%s", n, i+1, ext)
		} else {
			m.Name = cleanFolderName(fmt.Sprintf("%s - %02d%s", jobName, i+1, ext))
		}
	}
	return len(ordered)
}

// sortByPieceOrder orders entries by where their data sits in the NZB.
func sortByPieceOrder(res *job.Result, es []*job.Entry) {
	key := func(e *job.Entry) int {
		if len(e.Pieces) == 0 || e.Pieces[0].Source >= len(res.Sources) {
			return 1 << 30
		}
		return res.Sources[e.Pieces[0].Source].NZBIndex
	}
	for i := 1; i < len(es); i++ {
		for j := i; j > 0 && key(es[j]) < key(es[j-1]); j-- {
			es[j], es[j-1] = es[j-1], es[j]
		}
	}
}

// ---- virtual folders (cli_mount's custom folders) -------------------------

// VirtualFolder is an extra top folder listing the jobs that match every
// filter (filter type -> value), as cli_mount's custom folders.
type VirtualFolder map[string]string

type vfFilter struct {
	kind, value string
	re          *regexp.Regexp
	size        int64
	age         time.Duration
	count       int
}

// FilterTypes are the supported filter types.
var FilterTypes = []string{"include", "exclude", "starts_with", "ends_with", "not_starts_with", "not_ends_with",
	"regex", "not_regex", "exact_match", "not_exact_match", "size_gt", "size_lt", "last_added",
	"file_count_gt", "file_count_lt", "files_regex", "not_files_regex", "tags", "not_tags",
	"category", "not_category", "protocol"}

func compileFilters(vf VirtualFolder) ([]vfFilter, error) {
	var out []vfFilter
	for kind, v := range vf {
		f := vfFilter{kind: kind, value: v}
		var err error
		switch kind {
		case "regex", "not_regex", "files_regex", "not_files_regex":
			f.re, err = regexp.Compile(v)
		case "size_gt", "size_lt":
			f.size, err = ParseSize(v)
		case "last_added":
			f.age, err = ParseAge(v)
		case "file_count_gt", "file_count_lt":
			f.count, err = strconv.Atoi(strings.TrimSpace(v))
		default:
			known := false
			for _, t := range FilterTypes {
				known = known || t == kind
			}
			if !known {
				err = fmt.Errorf("unknown filter %q", kind)
			}
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", kind, err)
		}
		out = append(out, f)
	}
	return out, nil
}

// ParseSize reads "10GB", "500 MB", "1.5TiB" or plain bytes.
func ParseSize(s string) (int64, error) {
	s = strings.ToUpper(strings.ReplaceAll(strings.TrimSpace(s), " ", ""))
	mult := int64(1)
	for _, u := range []struct {
		suf string
		m   int64
	}{{"TIB", 1 << 40}, {"GIB", 1 << 30}, {"MIB", 1 << 20}, {"KIB", 1 << 10},
		{"TB", 1 << 40}, {"GB", 1 << 30}, {"MB", 1 << 20}, {"KB", 1 << 10}, {"T", 1 << 40}, {"G", 1 << 30},
		{"M", 1 << 20}, {"K", 1 << 10}, {"B", 1}} {
		if strings.HasSuffix(s, u.suf) {
			s, mult = strings.TrimSuffix(s, u.suf), u.m
			break
		}
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || f < 0 {
		return 0, fmt.Errorf("bad size %q", s)
	}
	return int64(f * float64(mult)), nil
}

// ParseAge reads Go durations plus d (days) and w (weeks): "7d", "36h".
func ParseAge(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	for suf, unit := range map[string]time.Duration{"d": 24 * time.Hour, "w": 7 * 24 * time.Hour} {
		if strings.HasSuffix(s, suf) {
			n, err := strconv.ParseFloat(strings.TrimSuffix(s, suf), 64)
			if err != nil {
				return 0, fmt.Errorf("bad duration %q", s)
			}
			return time.Duration(n * float64(unit)), nil
		}
	}
	return time.ParseDuration(s)
}

func anyTag(tags []string, want string) bool {
	for _, w := range strings.Split(want, ",") {
		w = strings.TrimSpace(w)
		for _, t := range tags {
			if w != "" && strings.EqualFold(strings.TrimSpace(t), w) {
				return true
			}
		}
	}
	return false
}

// matches says whether job j (shown as folder f) belongs in the folder.
// regex and files_regex together are OR (name or a file matches); every
// other filter must match.
func matches(fs []vfFilter, j store.Job, f *Folder) bool {
	files := func() []string {
		out := make([]string, len(f.Entries))
		for i, e := range f.Entries {
			out[i] = e.Name
		}
		return out
	}
	var size int64
	for _, e := range f.Entries {
		size += e.Size
	}
	one := func(x vfFilter) bool {
		name := f.Name
		switch x.kind {
		case "include":
			return strings.Contains(name, x.value)
		case "exclude":
			return !strings.Contains(name, x.value)
		case "starts_with":
			return strings.HasPrefix(name, x.value)
		case "ends_with":
			return strings.HasSuffix(name, x.value)
		case "not_starts_with":
			return !strings.HasPrefix(name, x.value)
		case "not_ends_with":
			return !strings.HasSuffix(name, x.value)
		case "regex":
			return x.re.MatchString(name)
		case "not_regex":
			return !x.re.MatchString(name)
		case "exact_match":
			return name == x.value
		case "not_exact_match":
			return name != x.value
		case "size_gt":
			return size > x.size
		case "size_lt":
			return size < x.size
		case "last_added":
			return time.Since(j.CreatedAt) <= x.age
		case "file_count_gt":
			return len(f.Entries) > x.count
		case "file_count_lt":
			return len(f.Entries) < x.count
		case "files_regex", "not_files_regex":
			hit := false
			for _, n := range files() {
				hit = hit || x.re.MatchString(n)
			}
			return hit == (x.kind == "files_regex")
		case "tags":
			return anyTag(j.Tags, x.value)
		case "not_tags":
			return !anyTag(j.Tags, x.value)
		case "category":
			return strings.EqualFold(j.Category, x.value)
		case "not_category":
			return !strings.EqualFold(j.Category, x.value)
		case "protocol": // usenet or debrid
			p := "usenet"
			if j.Protocol == ProtocolDebrid {
				p = "debrid"
			}
			return strings.EqualFold(p, strings.TrimSpace(x.value))
		}
		return false
	}
	var nameRe, filesRe, rest []vfFilter
	for _, x := range fs {
		switch x.kind {
		case "regex":
			nameRe = append(nameRe, x)
		case "files_regex":
			filesRe = append(filesRe, x)
		default:
			rest = append(rest, x)
		}
	}
	if len(nameRe) > 0 && len(filesRe) > 0 {
		hit := false
		for _, x := range append(nameRe, filesRe...) {
			hit = hit || one(x)
		}
		if !hit {
			return false
		}
	} else {
		rest = append(rest, append(nameRe, filesRe...)...)
	}
	for _, x := range rest {
		if !one(x) {
			return false
		}
	}
	return true
}

type namedFolder struct {
	name    string
	filters []vfFilter
}

// compileVirtualFolders checks the configured folders; a bad one is
// skipped (and logged) rather than stopping the mount.
func compileVirtualFolders(cfg map[string]VirtualFolder, log interface{ Warn(string, ...any) }) []namedFolder {
	var out []namedFolder
	for name, vf := range cfg {
		clean := cleanFolderName(name)
		reserved := clean == "" || clean == "_"
		for _, t := range fixedTops {
			reserved = reserved || strings.EqualFold(t, clean)
		}
		if reserved {
			log.Warn("virtual folder skipped: reserved name", "folder", name)
			continue
		}
		fs, err := compileFilters(vf)
		if err != nil || len(fs) == 0 {
			log.Warn("virtual folder skipped: bad filters", "folder", name, "err", err)
			continue
		}
		out = append(out, namedFolder{name: clean, filters: fs})
	}
	sortFolders(out)
	return out
}

func sortFolders(fs []namedFolder) {
	for i := 1; i < len(fs); i++ {
		for j := i; j > 0 && fs[j].name < fs[j-1].name; j-- {
			fs[j], fs[j-1] = fs[j-1], fs[j]
		}
	}
}

// HiddenByExtension reports whether res has files but the allowed
// extensions hide every one of them.
func (e *Engine) HiddenByExtension(res *job.Result) bool {
	if res == nil || len(res.Entries) == 0 || len(e.cfg.AllowedExt) == 0 {
		return false
	}
	for _, en := range res.Entries {
		if extAllowed(e.cfg.AllowedExt, en.Name) {
			return false
		}
	}
	return true
}

// entryExts lists the distinct extensions of entries, for error messages.
func entryExts(entries []job.Entry) []string {
	seen := map[string]bool{}
	var out []string
	for _, en := range entries {
		x := strings.TrimPrefix(strings.ToLower(path.Ext(en.Name)), ".")
		if x == "" {
			x = "(none)"
		}
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	sort.Strings(out)
	return out
}
