package stream

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"io"
	"math/rand"
	"testing"

	"github.com/godver3/cli_debrid/engine/internal/nntp"
)

type memSrc []byte

func (m memSrc) ReadAtCtx(_ context.Context, _ nntp.Priority, p []byte, off int64) (int, error) {
	if off >= int64(len(m)) {
		return 0, io.EOF
	}
	n := copy(p, m[off:])
	if n < len(p) {
		return n, io.EOF
	}
	return n, nil
}

func encrypt(t *testing.T, plain, key, iv []byte) []byte {
	t.Helper()
	padded := make([]byte, (len(plain)+15)/16*16)
	copy(padded, plain)
	b, err := aes.NewCipher(key)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]byte, len(padded))
	cipher.NewCBCEncrypter(b, iv).CryptBlocks(out, padded)
	return out
}

func TestDecryptedFileRandomReads(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	plain := make([]byte, 100_003) // not a whole number of blocks, like a real member
	rng.Read(plain)
	key, iv := make([]byte, 32), make([]byte, 16)
	rng.Read(key)
	rng.Read(iv)
	ct := encrypt(t, plain, key, iv)
	d, err := NewDecryptedFile(memSrc(ct), Encryption{Key: key, IV: iv}, int64(len(plain)), int64(len(ct)))
	if err != nil {
		t.Fatal(err)
	}
	all := make([]byte, len(plain))
	if n, err := d.ReadAtCtx(context.Background(), nntp.Live, all, 0); n != len(plain) || err != nil || !bytes.Equal(all, plain) {
		t.Fatalf("full read n=%d err=%v equal=%v", n, err, bytes.Equal(all, plain))
	}
	for i := 0; i < 2000; i++ {
		off := rng.Int63n(int64(len(plain)))
		p := make([]byte, 1+rng.Intn(5000))
		n, err := d.ReadAtCtx(context.Background(), nntp.Live, p, off)
		want := plain[off:min(off+int64(len(p)), int64(len(plain)))]
		if n != len(want) || !bytes.Equal(p[:n], want) {
			t.Fatalf("read off=%d len=%d: n=%d err=%v", off, len(p), n, err)
		}
		if n < len(p) && err != io.EOF {
			t.Fatalf("short read without EOF at %d", off)
		}
	}
	if _, err := d.ReadAtCtx(context.Background(), nntp.Live, make([]byte, 1), int64(len(plain))); err != io.EOF {
		t.Fatalf("read past end: %v", err)
	}
}

func TestDecryptedFileRejectsBadParams(t *testing.T) {
	if _, err := NewDecryptedFile(memSrc(nil), Encryption{Key: make([]byte, 31), IV: make([]byte, 16)}, 0, 0); err == nil {
		t.Fatal("bad key size accepted")
	}
	if _, err := NewDecryptedFile(memSrc(nil), Encryption{Key: make([]byte, 32), IV: make([]byte, 8)}, 0, 0); err == nil {
		t.Fatal("bad IV size accepted")
	}
	if _, err := NewDecryptedFile(memSrc(make([]byte, 32)), Encryption{Key: make([]byte, 32), IV: make([]byte, 16)}, 40, 32); err == nil {
		t.Fatal("plaintext larger than ciphertext accepted")
	}
}

func TestDecryptedFileTruncatedSourceIsLayoutError(t *testing.T) {
	key, iv := make([]byte, 32), make([]byte, 16)
	ct := encrypt(t, make([]byte, 64), key, iv)
	d, _ := NewDecryptedFile(memSrc(ct[:40]), Encryption{Key: key, IV: iv}, 64, 64)
	if _, err := d.ReadAtCtx(context.Background(), nntp.Live, make([]byte, 16), 48); err == nil {
		t.Fatal("truncated ciphertext read succeeded")
	}
}
