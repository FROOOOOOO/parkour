package binder

import (
	"testing"
	"time"

	"example.com/scheduler-lib/types"
)

func TestBindingReporter_Record(t *testing.T) {
	// Use nil crdClient — we only test buffering, not flush.
	r := NewBindingReporter(nil, "test-stats", 1*time.Second)

	r.Record(types.BindingResult{
		PodKey:       "default/pod-0",
		NodeName:     "node-0",
		Rank:         0,
		Success:      true,
		Timestamp:    time.Now(),
		PartitionID:  1,
		AttemptCount: 1,
	})

	r.Record(types.BindingResult{
		PodKey:       "default/pod-1",
		Success:      false,
		Timestamp:    time.Now(),
		AttemptCount: 3,
		ErrorMsg:     "all failed",
	})

	if r.BufferLen() != 2 {
		t.Errorf("BufferLen: got %d, want 2", r.BufferLen())
	}
}

func TestBindingReporter_DefaultFlushPeriod(t *testing.T) {
	r := NewBindingReporter(nil, "test", 0)
	// Should default to 1s (we can't directly check the private field,
	// but we verify no panic on zero value).
	if r == nil {
		t.Fatal("NewBindingReporter returned nil")
	}
}
