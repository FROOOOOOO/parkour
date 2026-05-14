package storage

import (
	"context"

	"example.com/scheduler-lib/types"
)

// Storage is the primary storage interface.
type Storage interface {
	// Adoption-stats storage.
	SaveAdoptionStats(ctx context.Context, stats *types.AdoptionStats) error
	LoadAdoptionStats(ctx context.Context) (*types.AdoptionStats, error)

	// Partition-assignment storage.
	SavePartitionAssignment(ctx context.Context, assignment *types.PartitionAssignment) error
	LoadPartitionAssignment(ctx context.Context, schedulerID int) (*types.PartitionAssignment, error)

	// Sync-state storage.
	SaveSyncState(ctx context.Context, schedulerID int, state *SyncState) error
	LoadSyncState(ctx context.Context, schedulerID int) (*SyncState, error)
}

// SyncState holds the synchronisation state for a scheduler.
type SyncState struct {
	SchedulerID      int
	LastSyncTime     map[int]int64 // Unix nanoseconds
	SyncGenerations  map[int]int64
	ConfigGeneration int64
}

// StatsStorage is a specialised storage interface for statistics counters.
type StatsStorage interface {
	// Save/load global counts.
	SaveGlobalCounts(ctx context.Context, counts []int64) error
	LoadGlobalCounts(ctx context.Context) ([]int64, error)

	// Save/load per-partition counts.
	SavePartitionCounts(ctx context.Context, partitionID int, counts []int64) error
	LoadPartitionCounts(ctx context.Context, partitionID int) ([]int64, error)

	// Atomically increment a count.
	IncrementCount(ctx context.Context, partitionID int, rank int) error
}
