package parsync

import (
	"time"

	"example.com/scheduler-lib/types"
)

// SyncScheduler computes sync time slots for partitions.
type SyncScheduler struct {
	numPartitions int
	numSchedulers int
	syncPeriod    time.Duration
	slotInterval  time.Duration
	clockBase     time.Time
}

// NewSyncScheduler creates a SyncScheduler.
func NewSyncScheduler(numPartitions, numSchedulers int, syncPeriod time.Duration) *SyncScheduler {
	slotInterval := syncPeriod / time.Duration(numSchedulers)

	return &SyncScheduler{
		numPartitions: numPartitions,
		numSchedulers: numSchedulers,
		syncPeriod:    syncPeriod,
		slotInterval:  slotInterval,
		clockBase:     time.Now().Truncate(syncPeriod),
	}
}

// SetClockBase sets the clock base for global synchronisation.
func (ss *SyncScheduler) SetClockBase(base time.Time) {
	ss.clockBase = base.Truncate(ss.syncPeriod)
}

// CalculateSchedule computes the sync timetable for the given scheduler and
// its assigned partitions.
func (ss *SyncScheduler) CalculateSchedule(schedulerID int, assignedPartitions []int) *types.SyncSchedule {
	slots := make([]types.SyncSlot, len(assignedPartitions))

	for i, partitionID := range assignedPartitions {
		// Compute the sync offset for this partition.
		// offset = slotIndex * slotInterval
		offset := time.Duration(i) * ss.slotInterval

		slots[i] = types.SyncSlot{
			SlotIndex:   i,
			PartitionID: partitionID,
			Offset:      offset,
		}
	}

	return &types.SyncSchedule{
		SchedulerID:  schedulerID,
		Slots:        slots,
		SyncPeriod:   ss.syncPeriod,
		SlotInterval: ss.slotInterval,
	}
}

// GetNextSyncTime returns the next scheduled sync time for the given partition.
func (ss *SyncScheduler) GetNextSyncTime(schedule *types.SyncSchedule, partitionID int) time.Time {
	now := time.Now()

	// Find the slot for this partition.
	var slot *types.SyncSlot
	for i := range schedule.Slots {
		if schedule.Slots[i].PartitionID == partitionID {
			slot = &schedule.Slots[i]
			break
		}
	}

	if slot == nil {
		return time.Time{} // this partition does not belong to this scheduler
	}

	// Determine the current cycle.
	elapsed := now.Sub(ss.clockBase)
	currentCycle := elapsed / ss.syncPeriod
	cycleStart := ss.clockBase.Add(currentCycle * ss.syncPeriod)

	// Compute the sync time within the current cycle.
	syncTime := cycleStart.Add(slot.Offset)

	if syncTime.Before(now) {
		// Current cycle already passed; return the next cycle's time.
		syncTime = syncTime.Add(ss.syncPeriod)
	}

	return syncTime
}

// GetDelayUntilNextSync returns the duration until the next sync for the given
// partition.
func (ss *SyncScheduler) GetDelayUntilNextSync(schedule *types.SyncSchedule, partitionID int) time.Duration {
	nextSync := ss.GetNextSyncTime(schedule, partitionID)
	if nextSync.IsZero() {
		return 0
	}

	delay := time.Until(nextSync)
	if delay < 0 {
		delay = 0
	}
	return delay
}

// CalculateFreshPartition returns the currently freshest partition for the
// given scheduler, based on the ParSync rotation formula.
func (ss *SyncScheduler) CalculateFreshPartition(schedulerID int) int {
	now := time.Now()

	// Compute the current time slot.
	elapsed := now.Sub(ss.clockBase)
	slot := int64(elapsed / ss.slotInterval)

	// ParSync formula: fresh_partition = (schedulerID + slot) % numPartitions
	freshPartition := (schedulerID + int(slot)) % ss.numPartitions

	return freshPartition
}

// GetFreshPartitionsForScheduler returns the assigned partitions sorted by
// freshness (freshest first).
func (ss *SyncScheduler) GetFreshPartitionsForScheduler(schedulerID int, assignedPartitions []int) []int {
	if len(assignedPartitions) == 0 {
		return nil
	}

	freshest := ss.CalculateFreshPartition(schedulerID)

	// Sort the assigned partitions with the freshest one first.
	result := make([]int, len(assignedPartitions))
	copy(result, assignedPartitions)

	// Simple sort: move the freshest partition to the front.
	for i := 0; i < len(result); i++ {
		if result[i] == freshest {
			result[0], result[i] = result[i], result[0]
			break
		}
	}

	return result
}
