package nntptest

import (
	"bufio"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// Server is a minimal NNTP server: greeting, AUTHINFO, BODY, STAT, QUIT.
type Server struct {
	ln       net.Listener
	mu       sync.Mutex
	articles map[string][]byte

	// Settings below are changed through the setters (they lock).
	user, pass string        // when set, BODY/STAT need AUTHINFO first
	delay      time.Duration // slept before answering each BODY
	drop       map[string]bool

	Bodies  atomic.Int64 // BODY commands answered with an article
	Stats   atomic.Int64
	Accepts atomic.Int64
	// Order records the ids of BODY commands in the order received.
	Order []string
}

// Start runs a server on a random local port until the test ends.
func Start(t testing.TB, arts ...Article) *Server {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{ln: ln, articles: map[string][]byte{}, drop: map[string]bool{}}
	s.Add(arts...)
	go s.serve()
	t.Cleanup(func() { ln.Close() })
	return s
}

// Add makes articles available.
func (s *Server) Add(arts ...Article) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, a := range arts {
		s.articles["<"+a.ID+">"] = a.Body
	}
}

// SetAuth requires AUTHINFO USER/PASS before BODY and STAT.
func (s *Server) SetAuth(user, pass string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.user, s.pass = user, pass
}

// SetDelay sleeps before answering each BODY.
func (s *Server) SetDelay(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.delay = d
}

// Drop makes the server close the connection instead of answering BODY for
// id (a flaky provider, not a missing article).
func (s *Server) Drop(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.drop[id] = true
}

// Undrop answers BODY for id normally again.
func (s *Server) Undrop(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.drop, id)
}

// Remove makes an article answer 430.
func (s *Server) Remove(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.articles, "<"+id+">")
}

// Addr returns host and port.
func (s *Server) Addr() (string, int) {
	a := s.ln.Addr().(*net.TCPAddr)
	return a.IP.String(), a.Port
}

func (s *Server) serve() {
	for {
		c, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.Accepts.Add(1)
		go s.handle(c)
	}
}

func (s *Server) handle(c net.Conn) {
	defer c.Close()
	r := bufio.NewReader(c)
	write := func(line string) bool {
		_, err := c.Write([]byte(line + "\r\n"))
		return err == nil
	}
	if !write("200 nntptest ready") {
		return
	}
	s.mu.Lock()
	wantUser, wantPass := s.user, s.pass
	s.mu.Unlock()
	authed := wantUser == ""
	user := ""
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return
		}
		f := strings.Fields(strings.TrimSpace(line))
		if len(f) == 0 {
			continue
		}
		cmd := strings.ToUpper(f[0])
		arg := ""
		if len(f) > 1 {
			arg = f[len(f)-1]
		}
		switch {
		case cmd == "QUIT":
			write("205 bye")
			return
		case cmd == "AUTHINFO" && len(f) == 3 && strings.EqualFold(f[1], "USER"):
			user = f[2]
			write("381 password required")
		case cmd == "AUTHINFO" && len(f) == 3 && strings.EqualFold(f[1], "PASS"):
			if user == wantUser && f[2] == wantPass {
				authed = true
				write("281 ok")
			} else {
				write("481 bad credentials")
			}
		case !authed:
			write("480 authentication required")
		case cmd == "STAT":
			s.Stats.Add(1)
			s.mu.Lock()
			_, ok := s.articles[arg]
			s.mu.Unlock()
			if ok {
				write("223 0 " + arg)
			} else {
				write("430 no such article")
			}
		case cmd == "BODY":
			s.mu.Lock()
			body, ok := s.articles[arg]
			s.Order = append(s.Order, strings.Trim(arg, "<>"))
			drop := s.drop[strings.Trim(arg, "<>")]
			delay := s.delay
			s.mu.Unlock()
			if drop {
				return
			}
			if delay > 0 {
				time.Sleep(delay)
			}
			if !ok {
				write("430 no such article")
				continue
			}
			s.Bodies.Add(1)
			if !write("222 0 " + arg) {
				return
			}
			if _, err := c.Write(body); err != nil {
				return
			}
		default:
			write("500 unknown command")
		}
	}
}

// BodyOrder returns a copy of the BODY ids received so far.
func (s *Server) BodyOrder() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.Order...)
}
