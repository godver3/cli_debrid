package nntp

import (
	"errors"
	"fmt"
	"strings"
)

// ErrArticleNotFound means a provider answered that the article does not
// exist (430 / 423). It is proof, unlike a timeout or a dropped connection.
// Client only returns it when every provider gave that answer.
var ErrArticleNotFound = errors.New("nntp: article not found")

// ErrAuth means the provider rejected the credentials. Retrying the same
// provider is pointless until settings change.
var ErrAuth = errors.New("nntp: authentication failed")

// ErrBusy means the provider refused a connection because the account has
// too many open ("502 Too many connections", "481 max connections
// reached", "400 ... unavailable"). Not a login problem: the pool stops
// opening more for a while and the next provider is asked.
var ErrBusy = errors.New("nntp: too many connections")

// busyMsg: status texts providers use for a full account.
func busyMsg(msg string) bool {
	m := strings.ToLower(msg)
	for _, s := range []string{"too many", "connection limit", "max connections",
		"maximum connections", "maximum number of connections", "connections exceeded",
		"connections reached"} {
		if strings.Contains(m, s) {
			return true
		}
	}
	return false
}

// ErrNoProviders means the client has no usable provider at all.
var ErrNoProviders = errors.New("nntp: no providers configured")

// ProtocolError is any other unexpected status line.
type ProtocolError struct {
	Code int
	Msg  string
}

func (e *ProtocolError) Error() string { return fmt.Sprintf("nntp: %d %s", e.Code, e.Msg) }

// classify turns a non-success status into one of the errors above. A
// bare 400 is not "busy" here: on a connection already in use it is the
// server closing it ("400 Idle timeout"), handled as a stale connection.
func classify(code int, msg string) error {
	if code != 430 && code != 423 && busyMsg(msg) {
		return fmt.Errorf("%w: %d %s", ErrBusy, code, msg)
	}
	switch code {
	case 430, 423:
		return ErrArticleNotFound
	case 481, 482, 502:
		return fmt.Errorf("%w: %d %s", ErrAuth, code, msg)
	}
	return &ProtocolError{Code: code, Msg: msg}
}

// classifyConnect is classify for the greeting and login of a new
// connection, where "400 Service temporarily unavailable" means the
// server won't take another connection right now.
func classifyConnect(code int, msg string) error {
	if code == 400 {
		return fmt.Errorf("%w: %d %s", ErrBusy, code, msg)
	}
	return classify(code, msg)
}

// isClosing: the server answered 400 on a connection already in use, which
// means it is closing it (idle timeout, shutdown). Says nothing about the
// article or the account.
func isClosing(err error) bool {
	var pe *ProtocolError
	return errors.As(err, &pe) && pe.Code == 400
}

// ErrTruncated: a server's copy of an article decodes shorter than its own
// yEnc trailer says. Treated like a missing article on that server.
var ErrTruncated = errors.New("article truncated on the server")

// ErrMismatch: the servers that had the Message-ID returned a different
// article than the file needs (the caller's accept check said no), and the
// rest said "no such article". One server's wrong copy is not this error:
// the next server is asked first (cli_mount's ArticleMismatch failover).
var ErrMismatch = errors.New("nntp: article does not match the file on any server")

// IsNotFound reports whether err is a confirmed missing article.
func IsNotFound(err error) bool { return errors.Is(err, ErrArticleNotFound) }
