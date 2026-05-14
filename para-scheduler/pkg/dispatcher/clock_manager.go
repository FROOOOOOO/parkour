package dispatcher

import (
	"sync"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
)

// ClockManager manages the global synchronization clock for ParSync.
// It maintains a monotonically increasing epoch counter and a clock base
// aligned to syncPeriod boundaries.
type ClockManager struct {
	mu           sync.RWMutex
	clockBase    time.Time     // aligned to syncPeriod boundary
	currentEpoch int64         // monotonically increasing epoch count
	syncPeriod   time.Duration // complete sync period G
}

// NewClockManager creates a ClockManager with the given sync period.
// The clock base is aligned to the nearest syncPeriod boundary.
func NewClockManager(syncPeriod time.Duration) *ClockManager {
	now := time.Now()
	return &ClockManager{
		clockBase:  alignToSyncPeriod(now, syncPeriod),
		syncPeriod: syncPeriod,
	}
}

// AdvanceEpoch increments the epoch counter and re-aligns the clock base.
// Should be called by Coordinator once per sync period G.
func (cm *ClockManager) AdvanceEpoch() {
	cm.mu.Lock()
	defer cm.mu.Unlock()

	cm.currentEpoch++
	cm.clockBase = cm.clockBase.Add(cm.syncPeriod)
}

// GetCurrentEpoch returns the current epoch number.
func (cm *ClockManager) GetCurrentEpoch() int64 {
	cm.mu.RLock()
	defer cm.mu.RUnlock()
	return cm.currentEpoch
}

// GetClockBase returns the current clock base time.
func (cm *ClockManager) GetClockBase() time.Time {
	cm.mu.RLock()
	defer cm.mu.RUnlock()
	return cm.clockBase
}

// CalculateSyncSlots computes the SyncSlot time offsets for a given scheduler.
//
// For diffSync (ParSync core pattern):
//
//	Scheduler j, Partition i:
//	  offset = ((i + j) % M) * slotInterval
//	  where slotInterval = G / M
//
// Parameters:
//   - schedulerID: numeric ID of the scheduler
//   - assignedPartitions: partitions assigned to this scheduler
//   - numPartitions: total number of partitions M
func (cm *ClockManager) CalculateSyncSlots(
	schedulerID int,
	assignedPartitions []int,
	numPartitions int,
) []apisv1.SyncSlotSpec {
	cm.mu.RLock()
	defer cm.mu.RUnlock()

	if numPartitions <= 0 {
		return nil
	}

	slotInterval := cm.syncPeriod / time.Duration(numPartitions)
	slots := make([]apisv1.SyncSlotSpec, len(assignedPartitions))

	for i, pid := range assignedPartitions {
		// diffSync offset formula: ((partitionID + schedulerID) % M) * slotInterval
		offset := time.Duration(((pid + schedulerID) % numPartitions)) * slotInterval
		slots[i] = apisv1.SyncSlotSpec{
			SlotIndex:    i,
			PartitionID:  pid,
			OffsetMillis: int64(offset / time.Millisecond),
		}
	}

	return slots
}

// CalculateSyncInterval returns the interval between partition syncs = syncPeriod / numPartitions.
func (cm *ClockManager) CalculateSyncInterval(numPartitions int) time.Duration {
	if numPartitions <= 0 {
		return cm.syncPeriod
	}
	return cm.syncPeriod / time.Duration(numPartitions)
}

// alignToSyncPeriod rounds down the given time to the nearest syncPeriod boundary
// based on Unix epoch.
func alignToSyncPeriod(t time.Time, period time.Duration) time.Time {
	if period <= 0 {
		return t
	}
	unixNano := t.UnixNano()
	periodNano := period.Nanoseconds()
	aligned := unixNano - (unixNano % periodNano)
	return time.Unix(0, aligned)
}
