package stream

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"fmt"
	"io"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

// ReaderAtCtx is any source the decrypting reader can sit on.
type ReaderAtCtx interface {
	ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error)
}

// Encryption is an AES-CBC key and IV for one encrypted archive member
// (RAR5: AES-256, key already derived from the password). The whole member
// is one CBC stream, the way cli_mount reads it.
type Encryption struct {
	Key []byte `json:"key,omitempty"`
	IV  []byte `json:"iv,omitempty"`
}

// Enabled reports whether there's anything to decrypt.
func (e Encryption) Enabled() bool { return len(e.Key) > 0 }

// Validate checks key and IV sizes.
func (e Encryption) Validate() error {
	switch len(e.Key) {
	case 16, 24, 32:
	default:
		return fmt.Errorf("stream: AES key is %d bytes, want 16, 24 or 32", len(e.Key))
	}
	if len(e.IV) != aes.BlockSize {
		return fmt.Errorf("stream: AES IV is %d bytes, want %d", len(e.IV), aes.BlockSize)
	}
	return nil
}

// DecryptedFile reads the plaintext of an AES-CBC encrypted source.
// The source holds the ciphertext padded to whole blocks; size is the
// plaintext size. Any offset can be read: block b decrypts with ciphertext
// block b-1 (or the IV for block 0) as its chaining value.
type DecryptedFile struct {
	src   ReaderAtCtx
	block cipher.Block
	iv    []byte
	size  int64
	enc   int64 // ciphertext size available in src
}

// NewDecryptedFile wraps src (encSize bytes of ciphertext) as size bytes of plaintext.
func NewDecryptedFile(src ReaderAtCtx, e Encryption, size, encSize int64) (*DecryptedFile, error) {
	if err := e.Validate(); err != nil {
		return nil, err
	}
	if size < 0 || size > encSize {
		return nil, fmt.Errorf("stream: plaintext size %d exceeds ciphertext size %d", size, encSize)
	}
	b, err := aes.NewCipher(e.Key)
	if err != nil {
		return nil, err
	}
	return &DecryptedFile{src: src, block: b, iv: append([]byte(nil), e.IV...), size: size, enc: encSize}, nil
}

// Size is the plaintext size.
func (d *DecryptedFile) Size() int64 { return d.size }

// ReadAtCtx reads len(p) plaintext bytes at off.
func (d *DecryptedFile) ReadAtCtx(ctx context.Context, prio nntp.Priority, p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("stream: negative offset")
	}
	if off >= d.size {
		return 0, io.EOF
	}
	end := min(off+int64(len(p)), d.size)
	const bs = aes.BlockSize
	aStart := off - off%bs
	aEnd := min((end+bs-1)/bs*bs, d.enc)
	from := aStart
	if aStart > 0 {
		from = aStart - bs // previous ciphertext block = chaining value
	}
	buf := make([]byte, aEnd-from)
	got, err := d.src.ReadAtCtx(ctx, prio, buf, from)
	if got < len(buf) {
		if err == nil || err == io.EOF {
			err = fmt.Errorf("%w: encrypted data ends at %d, need %d", ErrLayout, from+int64(got), aEnd)
		}
		return 0, err
	}
	iv, ct := d.iv, buf
	if aStart > 0 {
		iv, ct = buf[:bs], buf[bs:]
	}
	if len(ct)%bs != 0 {
		return 0, fmt.Errorf("%w: encrypted data is not whole AES blocks (%d bytes)", ErrLayout, len(ct))
	}
	plain := make([]byte, len(ct))
	cipher.NewCBCDecrypter(d.block, iv).CryptBlocks(plain, ct)
	n := copy(p, plain[off-aStart:end-aStart])
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}
