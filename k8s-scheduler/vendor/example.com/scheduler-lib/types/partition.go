package types

import "time"

// PartitionInfo 分区信息
type PartitionInfo struct {
	ID           int
	NodeCount    int
	LastSyncTime time.Time
	Generation   int64
	Staleness    time.Duration // 陈旧度
}

// SyncSlot 同步时间槽
type SyncSlot struct {
	SlotIndex   int
	PartitionID int
	Offset      time.Duration // 相对于周期起点的偏移
}

// SyncSchedule 同步调度表
type SyncSchedule struct {
	SchedulerID  int
	Slots        []SyncSlot
	SyncPeriod   time.Duration
	SlotInterval time.Duration
}

// PartitionAssignment 分区分配
type PartitionAssignment struct {
	SchedulerID        int
	AssignedPartitions []int
	ConfigGeneration   int64
}
