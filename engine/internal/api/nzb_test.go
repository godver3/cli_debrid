package api

import (
	"bytes"
	"io"
	"net/http"
	"testing"
)

func TestJobNZBIsHandedOut(t *testing.T) {
	h := newHarness(t)
	doc := nzbXML(h.s, "n", file{Name: "Movie.mkv", Data: random(7, 50_000)})
	var j jobOut
	if code := h.do("POST", "/jobs?name=Movie&category=movies", doc, &j); code != 201 {
		t.Fatalf("submit: %d", code)
	}
	req, _ := http.NewRequest("GET", h.srv.URL+"/jobs/"+j.ID+"/nzb", nil)
	req.Header.Set("Authorization", "Bearer secret")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !bytes.Equal(got, doc) || resp.Header.Get("Content-Type") != "application/x-nzb" {
		t.Fatalf("nzb: %d %q equal=%v", resp.StatusCode, resp.Header.Get("Content-Type"), bytes.Equal(got, doc))
	}
	if code := h.do("GET", "/jobs/nope/nzb", nil, nil); code != 404 {
		t.Fatalf("unknown job: %d", code)
	}
	req, _ = http.NewRequest("GET", h.srv.URL+"/jobs/"+j.ID+"/nzb", nil) // no token
	if resp, err := http.DefaultClient.Do(req); err != nil || resp.StatusCode != 401 {
		t.Fatalf("without the token: %v %v", resp.StatusCode, err)
	}
}

func TestDebridJobKeepsItsTorrentHash(t *testing.T) {
	h := newHarness(t)
	body := []byte(`{"id":"rd-ABC","name":"Movie","provider":"rd","hash":"ABCDEF0123","files":[{"name":"m.mkv","size":10,"ref":"rd:ABC:1"}]}`)
	var j jobOut
	if code := h.do("POST", "/debrid/jobs", body, &j); code != 201 || j.Hash != "abcdef0123" {
		t.Fatalf("%d %+v", code, j)
	}
}
