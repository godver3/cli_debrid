package nntp

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/godver3/cli_debrid/engine/internal/yenc"
)

// Provider is one usenet server account.
type Provider struct {
	Name     string `json:"name"`
	Host     string `json:"host"`
	Port     int    `json:"port"`
	TLS      bool   `json:"tls"`
	Username string `json:"username"`
	Password string `json:"password"`
	// MaxConns caps open connections to this provider.
	MaxConns int `json:"max_conns"`
	// Backup providers are only asked after every primary provider failed
	// to deliver an article (missing or unreachable).
	Backup bool `json:"backup"`
	// TimeoutSeconds bounds dial, each command, and each idle gap while
	// reading a body (default 30). Timeout overrides it (tests).
	TimeoutSeconds int           `json:"timeout_seconds"`
	Timeout        time.Duration `json:"-"`
	// Reserve keeps this many connections for live (playback) reads:
	// prefetch, imports' background work and health checks never take the
	// last Reserve connections. Clamped to MaxConns-1.
	Reserve int `json:"reserve"`
	// MinIdle connections are dialled ahead of time and kept open, so the
	// first read of a session skips the TCP/TLS/AUTH handshake.
	MinIdle int `json:"min_idle"`
	// Backbone names the article store this server shares with others
	// (e.g. "omicron"). When one server of a backbone answers "no such
	// article", the others on it have the same store and are not asked:
	// they count as having answered the same (cli_mount's backbone
	// exclusion). Empty = its own backbone.
	Backbone string `json:"backbone,omitempty"`
}

func (p Provider) backbone() string { return strings.ToLower(strings.TrimSpace(p.Backbone)) }

func (p Provider) addr() string { return net.JoinHostPort(p.Host, strconv.Itoa(p.Port)) }

func (p Provider) timeout() time.Duration {
	if p.Timeout > 0 {
		return p.Timeout
	}
	if p.TimeoutSeconds > 0 {
		return time.Duration(p.TimeoutSeconds) * time.Second
	}
	return 30 * time.Second
}

// Meta is the yEnc header of one article (one segment of a file).
type Meta struct {
	FileName string
	FileSize int64 // whole file, decoded
	Part     int64
	Total    int64
	Offset   int64 // where this part starts in the file (0-based)
	PartSize int64 // decoded size of this part
}

// conn is one authenticated NNTP connection. Not safe for concurrent use.
type conn struct {
	nc       net.Conn
	r        *bufio.Reader
	provider Provider
	lastUsed time.Time
	// lastBytes is the decoded size of the last BODY (0 after STAT), for
	// per-provider byte counts.
	lastBytes int
}

func dial(ctx context.Context, p Provider) (*conn, error) {
	d := net.Dialer{Timeout: p.timeout()}
	var nc net.Conn
	var err error
	if p.TLS {
		td := tls.Dialer{NetDialer: &d, Config: &tls.Config{ServerName: p.Host}}
		nc, err = td.DialContext(ctx, "tcp", p.addr())
	} else {
		nc, err = d.DialContext(ctx, "tcp", p.addr())
	}
	if err != nil {
		return nil, err
	}
	c := &conn{nc: nc, r: bufio.NewReaderSize(nc, 64*1024), provider: p}
	code, msg, err := c.readStatus()
	if err != nil {
		nc.Close()
		return nil, err
	}
	if code != 200 && code != 201 {
		nc.Close()
		return nil, classify(code, msg)
	}
	if p.Username != "" {
		if err := c.auth(); err != nil {
			nc.Close()
			return nil, err
		}
	}
	c.lastUsed = time.Now()
	return c, nil
}

func (c *conn) close() { _ = c.nc.Close() }

func (c *conn) deadline() { _ = c.nc.SetDeadline(time.Now().Add(c.provider.timeout())) }

func (c *conn) send(cmd string) error {
	c.deadline()
	_, err := io.WriteString(c.nc, cmd+"\r\n")
	return err
}

func (c *conn) readStatus() (int, string, error) {
	c.deadline()
	line, err := c.r.ReadString('\n')
	if err != nil {
		return 0, "", err
	}
	line = strings.TrimRight(line, "\r\n")
	if len(line) < 3 {
		return 0, "", fmt.Errorf("nntp: short status line %q", line)
	}
	code, err := strconv.Atoi(line[:3])
	if err != nil {
		return 0, "", fmt.Errorf("nntp: bad status line %q", line)
	}
	return code, strings.TrimSpace(line[3:]), nil
}

func (c *conn) auth() error {
	if err := c.send("AUTHINFO USER " + c.provider.Username); err != nil {
		return err
	}
	code, msg, err := c.readStatus()
	if err != nil {
		return err
	}
	if code == 281 {
		return nil
	}
	if code != 381 {
		return classify(code, msg)
	}
	if err := c.send("AUTHINFO PASS " + c.provider.Password); err != nil {
		return err
	}
	code, msg, err = c.readStatus()
	if err != nil {
		return err
	}
	if code != 281 {
		return fmt.Errorf("%w: %d %s", ErrAuth, code, msg)
	}
	return nil
}

func formatID(id string) string {
	id = strings.TrimSpace(id)
	if !strings.HasPrefix(id, "<") {
		id = "<" + id + ">"
	}
	return id
}

// body fetches and yEnc-decodes one article. On success the connection is
// positioned after the NNTP terminator and can be reused. Any error other
// than a clean status-code answer leaves the connection unusable.
func (c *conn) body(id string) ([]byte, Meta, error) {
	c.lastBytes = 0
	if err := c.send("BODY " + formatID(id)); err != nil {
		return nil, Meta{}, err
	}
	code, msg, err := c.readStatus()
	if err != nil {
		return nil, Meta{}, err
	}
	if code != 222 {
		return nil, Meta{}, classify(code, msg)
	}
	// Both decoder backends consume the raw NNTP stream: they undo
	// dot-stuffing and read through the ".\r\n" terminator themselves.
	dec := yenc.AcquireDecoder(&deadlineReader{c: c})
	defer yenc.ReleaseDecoder(dec)
	var out bytes.Buffer
	out.Grow(800 * 1024)
	if _, err := io.Copy(&out, dec); err != nil {
		if yenc.IsTruncated(err) {
			// The whole article (through =yend and the terminator) arrived
			// and decoded short: this server's copy is truncated.
			c.lastUsed = time.Now()
			return nil, Meta{}, fmt.Errorf("nntp: %s: %w: %v", id, ErrTruncated, err)
		}
		return nil, Meta{}, fmt.Errorf("nntp: decode %s: %w", id, err)
	}
	m := dec.Meta
	c.lastUsed = time.Now()
	c.lastBytes = out.Len()
	return out.Bytes(), Meta{
		FileName: m.FileName, FileSize: m.FileSize, Part: m.PartNumber,
		Total: m.TotalParts, Offset: m.Offset, PartSize: m.PartSize,
	}, nil
}

// stat asks whether an article exists without downloading it.
func (c *conn) stat(id string) (bool, error) {
	c.lastBytes = 0
	if err := c.send("STAT " + formatID(id)); err != nil {
		return false, err
	}
	code, msg, err := c.readStatus()
	if err != nil {
		return false, err
	}
	c.lastUsed = time.Now()
	switch code {
	case 223:
		return true, nil
	case 430, 423:
		return false, nil
	}
	return false, classify(code, msg)
}

// deadlineReader refreshes the idle deadline on every read, so a slow but
// moving transfer never times out while a stalled one does.
type deadlineReader struct{ c *conn }

func (d *deadlineReader) Read(p []byte) (int, error) {
	d.c.deadline()
	return d.c.r.Read(p)
}
