package engine

import (
	"path"
	"regexp"
	"strings"

	"github.com/godver3/cli_debrid/engine/internal/job"
)

// Extras naming. A media server pointed straight at the mount (cli_debrid's
// Plex mode) sees a job's whole folder: a movie torrent's bonus videos
// (Inception.Extras1...mkv) then become extra *versions* of the movie, and
// Play can pick one of them instead of the film. Plex and Jellyfin both read
// a "-featurette"/"-trailer"/... suffix as "this is an extra", so the mount
// shows such files with that suffix. Only the listed name changes; the data
// and the stored job don't.

var (
	reEpisodeName = regexp.MustCompile(`(?i)(^|[^a-z0-9])(s\d{1,3}[ ._-]?e\d{1,3}|\d{1,2}x\d{2})([^0-9]|$)`)
	reExtraSuffix = regexp.MustCompile(`(?i)-(trailer|deleted|behindthescenes|featurette|interview|scene|short|other)$`)
	extraKinds    = []struct {
		re     *regexp.Regexp
		suffix string
	}{
		{regexp.MustCompile(`(?i)(^|[^a-z])(trailers?|teasers?)([^a-z]|$)`), "trailer"},
		{regexp.MustCompile(`(?i)(^|[^a-z])deleted[ ._-]?scenes?([^a-z]|$)`), "deleted"},
		{regexp.MustCompile(`(?i)(^|[^a-z])(behind[ ._-]?the[ ._-]?scenes|making[ ._-]?of)([^a-z]|$)`), "behindthescenes"},
		{regexp.MustCompile(`(?i)(^|[^a-z])interviews?([^a-z]|$)`), "interview"},
		{regexp.MustCompile(`(?i)(^|[^a-z])(featurettes?|extras?\d*|bonus|special[ ._-]?features?)([^a-z]|$)`), "featurette"},
		{regexp.MustCompile(`(?i)(^|[^a-z])(gag[ ._-]?reels?|bloopers?|outtakes?)([^a-z]|$)`), "other"},
	}
)

// extraOtherShare: a video without an extras word still counts as an extra
// of a movie when it is below this share of the main file's size.
const extraOtherShare = 0.2

// markExtras returns entries with each extra video renamed
// "<base>-<kind><ext>". It never renames the main (largest) video or an
// episode, and leaves single-video jobs alone.
func markExtras(entries []job.Entry) []job.Entry {
	main := -1
	videos := 0
	for i, en := range entries {
		if !isVideo(en.Name) {
			continue
		}
		videos++
		if main < 0 || en.Size > entries[main].Size {
			main = i
		}
	}
	if videos < 2 {
		return entries
	}
	mainName := entries[main].Name
	movie := !reEpisodeName.MatchString(mainName)
	taken := make(map[string]bool, len(entries))
	for _, en := range entries {
		taken[strings.ToLower(en.Name)] = true
	}
	var out []job.Entry
	for i, en := range entries {
		if i == main || !isVideo(en.Name) || reEpisodeName.MatchString(en.Name) {
			continue
		}
		ext := path.Ext(en.Name)
		base := strings.TrimSuffix(en.Name, ext)
		if reExtraSuffix.MatchString(base) {
			continue
		}
		suffix := ""
		for _, k := range extraKinds {
			// A word the main file has too is part of the title ("Trailer
			// Park Boys"), not a sign of an extra.
			if k.re.MatchString(base) && !k.re.MatchString(strings.TrimSuffix(mainName, path.Ext(mainName))) {
				suffix = k.suffix
				break
			}
		}
		if suffix == "" && movie && float64(en.Size) < extraOtherShare*float64(entries[main].Size) {
			suffix = "other"
		}
		if suffix == "" {
			continue
		}
		name := base + "-" + suffix + ext
		if taken[strings.ToLower(name)] {
			continue
		}
		if out == nil {
			out = append([]job.Entry(nil), entries...)
		}
		taken[strings.ToLower(name)] = true
		out[i].Name = name
	}
	if out == nil {
		return entries
	}
	return out
}
