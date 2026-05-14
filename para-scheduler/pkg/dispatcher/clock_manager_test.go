package dispatcher

import (
	"testing"
	"time"
)

func TestNewClockManager(t *testing.T) {
	cm := NewClockManager(3 * time.Second)

	if cm.GetCurrentEpoch() != 0 {
		t.Errorf("initial epoch: got %d, want 0", cm.GetCurrentEpoch())
	}

	base := cm.GetClockBase()
	if base.IsZero() {
		t.Error("clock base should not be zero")
	}
}

func TestAdvanceEpoch(t *testing.T) {
	cm := NewClockManager(3 * time.Second)
	initialBase := cm.GetClockBase()

	cm.AdvanceEpoch()

	if cm.GetCurrentEpoch() != 1 {
		t.Errorf("epoch after advance: got %d, want 1", cm.GetCurrentEpoch())
	}

	newBase := cm.GetClockBase()
	diff := newBase.Sub(initialBase)
	if diff != 3*time.Second {
		t.Errorf("clock base should advance by syncPeriod: got %v, want 3s", diff)
	}

	cm.AdvanceEpoch()
	cm.AdvanceEpoch()
	if cm.GetCurrentEpoch() != 3 {
		t.Errorf("epoch after 3 advances: got %d, want 3", cm.GetCurrentEpoch())
	}
}

func TestCalculateSyncSlots_DiffPattern(t *testing.T) {
	cm := NewClockManager(3 * time.Second)
	numPartitions := 3

	// Scheduler 0, all partitions [0, 1, 2].
	slots0 := cm.CalculateSyncSlots(0, []int{0, 1, 2}, numPartitions)
	if len(slots0) != 3 {
		t.Fatalf("slots0 length: got %d, want 3", len(slots0))
	}

	// slotInterval = 3s / 3 = 1s = 1000ms.
	// Scheduler 0: offset = ((pid + 0) % 3) * 1000.
	// P0: 0ms, P1: 1000ms, P2: 2000ms.
	expectedOffsets0 := []int64{0, 1000, 2000}
	for i, s := range slots0 {
		if s.OffsetMillis != expectedOffsets0[i] {
			t.Errorf("slots0[%d].OffsetMillis: got %d, want %d", i, s.OffsetMillis, expectedOffsets0[i])
		}
		if s.PartitionID != i {
			t.Errorf("slots0[%d].PartitionID: got %d, want %d", i, s.PartitionID, i)
		}
	}

	// Scheduler 1: offset = ((pid + 1) % 3) * 1000.
	// P0: ((0+1)%3)*1000 = 1000, P1: ((1+1)%3)*1000 = 2000, P2: ((2+1)%3)*1000 = 0.
	slots1 := cm.CalculateSyncSlots(1, []int{0, 1, 2}, numPartitions)
	expectedOffsets1 := []int64{1000, 2000, 0}
	for i, s := range slots1 {
		if s.OffsetMillis != expectedOffsets1[i] {
			t.Errorf("slots1[%d].OffsetMillis: got %d, want %d", i, s.OffsetMillis, expectedOffsets1[i])
		}
	}
}

func TestCalculateSyncSlots_DiffPattern_NoOverlap(t *testing.T) {
	cm := NewClockManager(4 * time.Second)
	numPartitions := 4
	numSchedulers := 4
	partitions := []int{0, 1, 2, 3}

	// At each time offset, verify each scheduler syncs a different partition.
	// Collect offsets per scheduler.
	type slotKey struct {
		schedulerID int
		offset      int64
	}
	offsetToPartition := make(map[slotKey]int)

	for j := 0; j < numSchedulers; j++ {
		slots := cm.CalculateSyncSlots(j, partitions, numPartitions)
		for _, s := range slots {
			key := slotKey{j, s.OffsetMillis}
			offsetToPartition[key] = s.PartitionID
		}
	}

	// At each offset, check no two schedulers sync the same partition.
	slotInterval := int64(4000 / numPartitions) // 1000ms
	for offset := int64(0); offset < 4000; offset += slotInterval {
		seen := make(map[int]int) // partitionID -> schedulerID
		for j := 0; j < numSchedulers; j++ {
			key := slotKey{j, offset}
			pid := offsetToPartition[key]
			if prevJ, ok := seen[pid]; ok {
				t.Errorf("offset %dms: schedulers %d and %d both sync partition %d",
					offset, prevJ, j, pid)
			}
			seen[pid] = j
		}
	}
}

func TestCalculateSyncSlots_EmptyPartitions(t *testing.T) {
	cm := NewClockManager(3 * time.Second)
	slots := cm.CalculateSyncSlots(0, []int{}, 3)
	if len(slots) != 0 {
		t.Errorf("expected 0 slots for empty partitions, got %d", len(slots))
	}
}

func TestCalculateSyncSlots_ZeroPartitions(t *testing.T) {
	cm := NewClockManager(3 * time.Second)
	slots := cm.CalculateSyncSlots(0, []int{0}, 0)
	if slots != nil {
		t.Errorf("expected nil for 0 numPartitions, got %v", slots)
	}
}

func TestCalculateSyncInterval(t *testing.T) {
	cm := NewClockManager(3 * time.Second)

	interval := cm.CalculateSyncInterval(3)
	if interval != 1*time.Second {
		t.Errorf("interval: got %v, want 1s", interval)
	}

	interval = cm.CalculateSyncInterval(1)
	if interval != 3*time.Second {
		t.Errorf("interval for 1 partition: got %v, want 3s", interval)
	}

	interval = cm.CalculateSyncInterval(0)
	if interval != 3*time.Second {
		t.Errorf("interval for 0 partitions: got %v, want 3s", interval)
	}
}

func TestAlignToSyncPeriod(t *testing.T) {
	period := 3 * time.Second
	// Use a known time.
	ref := time.Unix(10, 500*int64(time.Millisecond))
	aligned := alignToSyncPeriod(ref, period)

	// 10.5s → should align to 9s (3*3=9).
	expected := time.Unix(9, 0)
	if !aligned.Equal(expected) {
		t.Errorf("aligned: got %v, want %v", aligned, expected)
	}
}

func TestAlignToSyncPeriod_ExactBoundary(t *testing.T) {
	period := 3 * time.Second
	ref := time.Unix(9, 0) // exactly on boundary
	aligned := alignToSyncPeriod(ref, period)

	if !aligned.Equal(ref) {
		t.Errorf("exact boundary: got %v, want %v", aligned, ref)
	}
}

func TestAlignToSyncPeriod_ZeroPeriod(t *testing.T) {
	ref := time.Unix(10, 0)
	aligned := alignToSyncPeriod(ref, 0)
	if !aligned.Equal(ref) {
		t.Errorf("zero period: got %v, want %v (unchanged)", aligned, ref)
	}
}
