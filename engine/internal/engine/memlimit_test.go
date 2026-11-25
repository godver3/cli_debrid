package engine

import "testing"

// The GC limit follows the memory cache (found live: 2.7 GB heap for a
// 1 GB cache under the default GOGC); an explicit value wins, -1 = off.
func TestMemoryLimit(t *testing.T) {
	c := Config{MemCacheBytes: 1 << 30}
	if got := c.memoryLimit(); got != 1<<30+768<<20 {
		t.Fatalf("default: %d", got)
	}
	c.MemoryLimitMB = 3000
	if got := c.memoryLimit(); got != 3000<<20 {
		t.Fatalf("set: %d", got)
	}
	c.MemoryLimitMB = -1
	if got := c.memoryLimit(); got != 0 {
		t.Fatalf("off: %d", got)
	}
}
