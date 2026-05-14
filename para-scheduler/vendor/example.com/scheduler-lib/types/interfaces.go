package types

import "time"

// SchedulerAdapter is implemented by concrete schedulers to supply the
// information required by the core library.
type SchedulerAdapter interface {
	// GetNodeScores returns the list of node scores.
	GetNodeScores() []NodeScore

	// GetClusterSize returns the number of nodes in the cluster.
	GetClusterSize() int

	// GetSchedulerID returns the scheduler ID.
	GetSchedulerID() int

	// IsHighPriorityPod reports whether the current pod is high-priority.
	IsHighPriorityPod() bool

	// GetPodKey returns the pod identifier.
	GetPodKey() string
}

// PartitionStateProvider provides partition-state information to the selector.
type PartitionStateProvider interface {
	// GetPartitionID returns the partition that the given node belongs to.
	GetPartitionID(nodeName string) int

	// GetPartitionStaleness returns how stale the given partition is.
	GetPartitionStaleness(partitionID int) time.Duration

	// GetFreshPartitions returns the fresh partitions for the current scheduler.
	GetFreshPartitions() []int

	// GetPartitionInfo returns metadata for the given partition.
	GetPartitionInfo(partitionID int) *PartitionInfo
}

// AdoptionStatsProvider supplies adoption statistics to scoring strategies.
type AdoptionStatsProvider interface {
	// GetNodeConflictRate returns the recent conflict rate [0,1] for the given
	// node.  Returns 0 during cold-start or when no samples are available,
	// used by QualityFirst / WeightedRandom scoring formulas.
	GetNodeConflictRate(nodeName string) float64

	// UpdateResult records a binding result.
	UpdateResult(result BindingResult)

	// GetStats returns a summary of adoption statistics.
	GetStats() *AdoptionStats
}

// AdoptionStats is a summary of adoption statistics.
type AdoptionStats struct {
	TotalBindings    int64
	SuccessCount     int64
	FailureCount     int64
	RankDistribution []int64 // number of times each rank was adopted
	AvgAttempts      float64
	ConflictRate     float64
}

// SyncSchedulerProvider provides sync-scheduling information.
type SyncSchedulerProvider interface {
	// GetNextSyncTime returns the next sync time for the given partition.
	GetNextSyncTime(partitionID int) time.Time

	// GetSyncSchedule returns the sync timetable.
	GetSyncSchedule() *SyncSchedule

	// ShouldSyncNow reports whether the given partition should be synced immediately.
	ShouldSyncNow(partitionID int) bool
}
