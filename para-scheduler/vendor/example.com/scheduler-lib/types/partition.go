package types

import "time"

// PartitionInfo holds metadata about a partition.
type PartitionInfo struct {
	ID           int
	NodeCount    int
	LastSyncTime time.Time
	Generation   int64
	Staleness    time.Duration // staleness of the partition
}

// SyncSlot represents a time slot assigned for syncing a partition.
type SyncSlot struct {
	SlotIndex   int
	PartitionID int
	Offset      time.Duration // offset relative to the start of the sync period
}

// SyncSchedule is the sync timetable for a scheduler.
type SyncSchedule struct {
	SchedulerID  int
	Slots        []SyncSlot
	SyncPeriod   time.Duration
	SlotInterval time.Duration
}

// PartitionAssignment describes which partitions a scheduler is responsible for.
type PartitionAssignment struct {
	SchedulerID        int
	AssignedPartitions []int
	ConfigGeneration   int64
}
