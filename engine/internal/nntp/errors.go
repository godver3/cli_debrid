package nntp

import (
	"errors"
	"fmt"
)

// ErrArticleNotFound means a provider answered that the article does not
// exist (430 / 423). It is proof, unlike a timeout or a dropped connection.
// Client only returns it when every provider gave that answer.
var ErrArticleNotFound = errors.New("nntp: article not found")

// ErrAuth means the provider rejected the credentials. Retrying the same
// provider is pointless until settings change.
var ErrAuth = errors.New("nntp: authentication failed")

// ErrNoProviders means the client has no usable provider at all.
var ErrNoProviders = errors.New("nntp: no providers configured")

// ProtocolError is any other unexpected status line.
type ProtocolError struct {
	Code int
	Msg  string
}

func (e *ProtocolError) Error() string { return fmt.Sprintf("nntp: %d %s", e.Code, e.Msg) }

// classify turns a non-success status into one of the errors above.
func classify(code int, msg string) error {
	switch code {
	case 430, 423:
		return ErrArticleNotFound
	case 481, 482, 502:
		return fmt.Errorf("%w: %d %s", ErrAuth, code, msg)
	}
	return &ProtocolError{Code: code, Msg: msg}
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
