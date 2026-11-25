package archive

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/binary"
	"errors"
	"fmt"
	"sync"
	"unicode/utf16"
)

// ErrWrongPassword means the archive is encrypted and the password given
// does not open it (checked against the archive's password check value).
var ErrWrongPassword = errors.New("archive: wrong password")

// Encryption is how to read an encrypted member: its extents hold AES-CBC
// ciphertext that starts with IV as the chaining value, and the member's
// plaintext begins Skip bytes into the decrypted stream (7z folders that
// hold several files; always 0 for RAR).
type Encryption struct {
	Key  []byte
	IV   []byte
	Skip int64
}

// ---- RAR 5 (AES-256-CBC, PBKDF2-HMAC-SHA256) -------------------------------

const (
	rar5KdfMax     = 24 // largest log2 iteration count RAR allows
	rar5SaltLen    = 16
	rar5CheckLen   = 12 // 8 check bytes + 4 SHA-256 checksum bytes
	rar5CryptPwChk = 0x01
)

// rar5Crypt is the encryption record of a RAR5 archive (encryption header)
// or file (extra area record).
type rar5Crypt struct {
	Kdf   int
	Salt  []byte
	IV    []byte // file records only
	Check []byte // nil when the archive carries no password check
}

// parseRAR5Crypt reads an encryption record body. withIV is true for the
// file record, which carries the data IV after the salt.
func parseRAR5Crypt(b []byte, withIV bool) (*rar5Crypt, error) {
	f := &fields{b: b}
	if v := f.next(); f.err == nil && v != 0 {
		return nil, fmt.Errorf("%w: RAR5 encryption version %d not supported", ErrEncrypted, v)
	}
	flags := f.next()
	kdf := f.bytes(1)
	salt := f.bytes(rar5SaltLen)
	c := &rar5Crypt{}
	if withIV {
		c.IV = f.bytes(aes.BlockSize)
	}
	if flags&rar5CryptPwChk != 0 {
		c.Check = f.bytes(rar5CheckLen)
	}
	if f.err != nil {
		return nil, f.err
	}
	c.Kdf = int(kdf[0])
	if c.Kdf > rar5KdfMax {
		return nil, fmt.Errorf("%w: RAR5 KDF count %d", ErrCorrupt, c.Kdf)
	}
	c.Salt = append([]byte(nil), salt...)
	c.IV = append([]byte(nil), c.IV...)
	c.Check = append([]byte(nil), c.Check...)
	return c, nil
}

type kdfKey struct {
	password, salt string
	kdf            int
}

type rar5Keys struct {
	key   []byte
	check []byte // 8-byte password check value
}

// Key derivation costs 2^kdf HMAC rounds (32768 by default); every volume
// of a set and every file in it usually share one salt, so cache.
var (
	keyMu    sync.Mutex
	keyCache = map[kdfKey]rar5Keys{}
)

const keyCacheMax = 256

// deriveRAR5 implements RAR 5.0's key derivation: PBKDF2-HMAC-SHA256 over
// the UTF-8 password, continued for 16 more rounds for the hash key and 16
// more again for the password check value (folded to 8 bytes).
func deriveRAR5(password string, salt []byte, kdf int) rar5Keys {
	ck := kdfKey{password, string(salt), kdf}
	keyMu.Lock()
	if k, ok := keyCache[ck]; ok {
		keyMu.Unlock()
		return k
	}
	keyMu.Unlock()

	prf := hmac.New(sha256.New, []byte(password))
	prf.Write(salt)
	prf.Write([]byte{0, 0, 0, 1})
	u := prf.Sum(nil)
	t := append([]byte(nil), u...)
	var out [3][]byte
	for i, rounds := range [3]int{1<<kdf - 1, 16, 16} {
		for ; rounds > 0; rounds-- {
			prf.Reset()
			prf.Write(u)
			u = prf.Sum(u[:0])
			for j := range t {
				t[j] ^= u[j]
			}
		}
		out[i] = append([]byte(nil), t...)
	}
	check := make([]byte, 8)
	for i, v := range out[2] {
		check[i%8] ^= v
	}
	k := rar5Keys{key: out[0], check: check}

	keyMu.Lock()
	if len(keyCache) >= keyCacheMax {
		clear(keyCache)
	}
	keyCache[ck] = k
	keyMu.Unlock()
	return k
}

// open derives the key for a record and verifies the password check value
// when the archive has one.
func (c *rar5Crypt) open(password string) ([]byte, error) {
	if password == "" {
		return nil, fmt.Errorf("%w: password needed", ErrEncrypted)
	}
	k := deriveRAR5(password, c.Salt, c.Kdf)
	if len(c.Check) == rar5CheckLen {
		sum := sha256.Sum256(c.Check[:8])
		// A check value whose own checksum doesn't match is damaged, not
		// proof of a wrong password: skip the check then (unrar does too).
		if subtle.ConstantTimeCompare(sum[:4], c.Check[8:]) == 1 &&
			subtle.ConstantTimeCompare(k.check, c.Check[:8]) != 1 {
			return nil, ErrWrongPassword
		}
	}
	return k.key, nil
}

// decryptCBC decrypts whole blocks in place.
func decryptCBC(key, iv, b []byte) error {
	if len(b)%aes.BlockSize != 0 {
		return fmt.Errorf("%w: encrypted header is not whole AES blocks", ErrCorrupt)
	}
	blk, err := aes.NewCipher(key)
	if err != nil {
		return err
	}
	cipher.NewCBCDecrypter(blk, iv).CryptBlocks(b, b)
	return nil
}

// ---- 7z (AES-256-CBC, iterated SHA-256 over salt + UTF-16LE password) -----

// sevenZipKey derives a 7z AES key. iterations is 2^cycles as the 7z
// reader reports it, 0 for the "no hashing" form (cycles 0x3f).
func sevenZipKey(password string, salt []byte, iterations int) []byte {
	ck := kdfKey{"7z\x00" + password, string(salt), iterations}
	keyMu.Lock()
	if k, ok := keyCache[ck]; ok {
		keyMu.Unlock()
		return k.key
	}
	keyMu.Unlock()

	buf := append([]byte(nil), salt...)
	for _, r := range utf16.Encode([]rune(password)) {
		buf = binary.LittleEndian.AppendUint16(buf, r)
	}
	key := make([]byte, sha256.Size)
	if iterations <= 0 {
		copy(key, buf)
	} else {
		h := sha256.New()
		var ctr [8]byte
		for i := 0; i < iterations; i++ {
			binary.LittleEndian.PutUint64(ctr[:], uint64(i))
			h.Write(buf)
			h.Write(ctr[:])
		}
		key = h.Sum(nil)
	}

	keyMu.Lock()
	if len(keyCache) >= keyCacheMax {
		clear(keyCache)
	}
	keyCache[ck] = rar5Keys{key: key}
	keyMu.Unlock()
	return key
}

// roundBlock rounds n up to whole AES blocks.
func roundBlock(n int64) int64 { return (n + aes.BlockSize - 1) / aes.BlockSize * aes.BlockSize }
