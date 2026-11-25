package store

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"os"
	"sort"

	"github.com/godver3/cli_debrid/engine/internal/job"
	"github.com/godver3/cli_debrid/engine/internal/stream"
)

// maps.bin holds the segment maps of a job's mapped sources (jobs migrated
// from cli_mount), so result.json and the in-memory store keep only what the
// mount listing needs. Maps are loaded when a job's files are opened or
// checked (Store.Full), like cli_mount reading a job's .meta on demand.
//
// A map is run-length encoded: consecutive articles almost always carry the
// same length and data offset (1.66M segments -> 26k runs on real jobs).
//
//	magic "CDMAP\x01"
//	uvarint  sources
//	  uvarint  source index (into Result.Sources)
//	  uvarint  runs
//	    varint   gap   (run start - end of the previous run; 0 in practice)
//	    uvarint  count (segments in the run)
//	    uvarint  len   (bytes per segment)
//	    uvarint  data start
//	uint32 LE CRC-32 (IEEE) of everything before it

const mapsFile = "maps.bin"

var mapsMagic = []byte("CDMAP\x01")

var errMapsCorrupt = errors.New("store: maps.bin is corrupt")

// encodeMaps encodes the maps of r's sources (sources without one are left
// out). ok is false when no source has a map.
func encodeMaps(r *job.Result) (data []byte, ok bool) {
	var idx []int
	for i, s := range r.Sources {
		if len(s.Map) > 0 {
			idx = append(idx, i)
		}
	}
	if len(idx) == 0 {
		return nil, false
	}
	b := append([]byte(nil), mapsMagic...)
	b = binary.AppendUvarint(b, uint64(len(idx)))
	for _, i := range idx {
		segs := r.Sources[i].Map
		b = binary.AppendUvarint(b, uint64(i))
		type run struct {
			gap               int64
			count, len, start int64
		}
		var runs []run
		var end int64
		for k, sg := range segs {
			gap := sg.Start - end
			if n := len(runs); n > 0 && gap == 0 && k > 0 && runs[n-1].len == sg.Len && runs[n-1].start == sg.DataStart {
				runs[n-1].count++
			} else {
				runs = append(runs, run{gap: gap, count: 1, len: sg.Len, start: sg.DataStart})
			}
			end = sg.Start + sg.Len
		}
		b = binary.AppendUvarint(b, uint64(len(runs)))
		for _, rn := range runs {
			b = binary.AppendVarint(b, rn.gap)
			b = binary.AppendUvarint(b, uint64(rn.count))
			b = binary.AppendUvarint(b, uint64(rn.len))
			b = binary.AppendUvarint(b, uint64(rn.start))
		}
	}
	return binary.LittleEndian.AppendUint32(b, crc32.ChecksumIEEE(b)), true
}

// decodeMaps returns source index -> map.
func decodeMaps(data []byte) (map[int][]stream.MapSeg, error) {
	if len(data) < len(mapsMagic)+4 || !bytes.HasPrefix(data, mapsMagic) {
		return nil, errMapsCorrupt
	}
	body, sum := data[:len(data)-4], binary.LittleEndian.Uint32(data[len(data)-4:])
	if crc32.ChecksumIEEE(body) != sum {
		return nil, errMapsCorrupt
	}
	b := body[len(mapsMagic):]
	uv := func() (uint64, error) {
		v, n := binary.Uvarint(b)
		if n <= 0 {
			return 0, errMapsCorrupt
		}
		b = b[n:]
		return v, nil
	}
	nsrc, err := uv()
	if err != nil {
		return nil, err
	}
	out := make(map[int][]stream.MapSeg, nsrc)
	for ; nsrc > 0; nsrc-- {
		src, err := uv()
		if err != nil {
			return nil, err
		}
		nruns, err := uv()
		if err != nil {
			return nil, err
		}
		var segs []stream.MapSeg
		var end int64
		for ; nruns > 0; nruns-- {
			gap, n := binary.Varint(b)
			if n <= 0 {
				return nil, errMapsCorrupt
			}
			b = b[n:]
			var v [3]uint64
			for k := range v {
				if v[k], err = uv(); err != nil {
					return nil, err
				}
			}
			count, sl, ds := v[0], int64(v[1]), int64(v[2])
			if count == 0 || count > 1<<28 {
				return nil, errMapsCorrupt
			}
			start := end + gap
			for ; count > 0; count-- {
				segs = append(segs, stream.MapSeg{Start: start, Len: sl, DataStart: ds})
				start += sl
			}
			end = start
		}
		out[int(src)] = segs
	}
	if len(b) != 0 {
		return nil, errMapsCorrupt
	}
	return out, nil
}

// splitMaps returns a copy of r without maps (MapsFile set when r had any)
// and the encoded maps.
func splitMaps(r *job.Result) (summary *job.Result, maps []byte) {
	maps, ok := encodeMaps(r)
	if !ok {
		return r, nil
	}
	sum := *r
	sum.Sources = make([]job.Source, len(r.Sources))
	copy(sum.Sources, r.Sources)
	for i := range sum.Sources {
		sum.Sources[i].Map = nil
	}
	sum.MapsFile = true
	return &sum, maps
}

// withMaps returns a copy of summary with the maps from maps.bin put back.
func withMaps(summary *job.Result, path string) (*job.Result, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	maps, err := decodeMaps(data)
	if err != nil {
		return nil, err
	}
	full := *summary
	full.Sources = make([]job.Source, len(summary.Sources))
	copy(full.Sources, summary.Sources)
	idx := make([]int, 0, len(maps))
	for i := range maps {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	for _, i := range idx {
		if i < 0 || i >= len(full.Sources) {
			return nil, fmt.Errorf("%w: map for source %d of %d", errMapsCorrupt, i, len(full.Sources))
		}
		full.Sources[i].Map = maps[i]
	}
	return &full, nil
}
