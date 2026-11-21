package par2

import (
	"crypto/md5"
	"encoding/binary"
	"math/rand"
	"testing"
)

// packet builds a PAR2 packet of the given type with a valid header.
func packet(typ string, body []byte) []byte {
	for len(body)%4 != 0 {
		body = append(body, 0)
	}
	p := make([]byte, headerLen, headerLen+len(body))
	copy(p, magic)
	binary.LittleEndian.PutUint64(p[8:], uint64(headerLen+len(body)))
	copy(p[48:], typ)
	p = append(p, body...)
	sum := md5.Sum(p[32:])
	copy(p[16:32], sum[:])
	return p
}

func fileDesc(id byte, name string, data []byte) []byte {
	body := make([]byte, 56)
	body[0] = id
	full := md5.Sum(data)
	copy(body[16:], full[:])
	h := Hash16k(data)
	copy(body[32:], h[:])
	binary.LittleEndian.PutUint64(body[48:], uint64(len(data)))
	return packet(fileDescType, append(body, name...))
}

func TestParseAndMatch(t *testing.T) {
	rnd := rand.New(rand.NewSource(1))
	movie := make([]byte, 100_000)
	rnd.Read(movie)
	vol2 := make([]byte, 50_000)
	rnd.Read(vol2)
	nfo := []byte("small file under 16k")

	var data []byte
	data = append(data, []byte("junk before the first packet PAR2\x00PK")...)
	data = append(data, packet("PAR 2.0\x00Main\x00\x00\x00\x00", make([]byte, 12))...)
	data = append(data, fileDesc(1, "Show.S01E01.part01.rar", movie)...)
	data = append(data, fileDesc(2, "Show.S01E01.part02.rar", vol2)...)
	data = append(data, fileDesc(3, "Show.S01E01.nfo", nfo)...)
	data = append(data, fileDesc(1, "Show.S01E01.part01.rar", movie)...) // repeated, as in every .par2 volume

	descs := Parse(data)
	if len(descs) != 3 {
		t.Fatalf("descs %d: %+v", len(descs), descs)
	}

	got := Match(descs, []Candidate{
		{Head: vol2[:HashLen], Size: int64(len(vol2))},   // obfuscated name, volume 2
		{Head: movie[:HashLen], Size: int64(len(movie))}, // volume 1
		{Head: nfo, Size: int64(len(nfo))},
		{Head: movie[:HashLen], Size: 12345}, // same start, wrong length: not a match
		{Head: movie[:100], Size: 0},         // too few bytes to hash
	})
	want := map[int]string{0: "Show.S01E01.part02.rar", 1: "Show.S01E01.part01.rar", 2: "Show.S01E01.nfo"}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i, n := range want {
		if got[i] != n {
			t.Fatalf("candidate %d: got %q, want %q", i, got[i], n)
		}
	}
}

func TestAmbiguousHashIsSkipped(t *testing.T) {
	same := make([]byte, HashLen)
	descs := Parse(append(fileDesc(1, "a.mkv", same), fileDesc(2, "b.mkv", same)...))
	if m := Match(descs, []Candidate{{Head: same, Size: int64(len(same))}}); len(m) != 0 {
		t.Fatalf("ambiguous match accepted: %v", m)
	}
}

func TestTruncatedPacketDoesNotPanic(t *testing.T) {
	p := fileDesc(1, "x.rar", []byte("abc"))
	for i := 0; i < len(p); i++ {
		Parse(p[:i])
	}
}
