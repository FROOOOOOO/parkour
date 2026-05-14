package storage

import (
	"context"
	"sync"

	"example.com/scheduler-lib/types"
)

// MemoryStorage is an in-memory implementation of Storage and StatsStorage.
type MemoryStorage struct {
	mu sync.RWMutex

	adoptionStats        *types.AdoptionStats
	partitionAssignments map[int]*types.PartitionAssignment
	syncStates           map[int]*SyncState

	globalCounts    []int64
	partitionCounts map[int][]int64
}

// NewMemoryStorage creates a new MemoryStorage.
func NewMemoryStorage() *MemoryStorage {
	return &MemoryStorage{
		partitionAssignments: make(map[int]*types.PartitionAssignment),
		syncStates:           make(map[int]*SyncState),
		partitionCounts:      make(map[int][]int64),
	}
}

// SaveAdoptionStats persists adoption statistics.
func (s *MemoryStorage) SaveAdoptionStats(ctx context.Context, stats *types.AdoptionStats) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Deep copy.
	statsCopy := *stats
	statsCopy.RankDistribution = make([]int64, len(stats.RankDistribution))
	copy(statsCopy.RankDistribution, stats.RankDistribution)

	s.adoptionStats = &statsCopy
	return nil
}

// LoadAdoptionStats returns the stored adoption statistics.
func (s *MemoryStorage) LoadAdoptionStats(ctx context.Context) (*types.AdoptionStats, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if s.adoptionStats == nil {
		return nil, nil
	}

	// Deep copy.
	statsCopy := *s.adoptionStats
	statsCopy.RankDistribution = make([]int64, len(s.adoptionStats.RankDistribution))
	copy(statsCopy.RankDistribution, s.adoptionStats.RankDistribution)

	return &statsCopy, nil
}

// SavePartitionAssignment persists a partition assignment.
func (s *MemoryStorage) SavePartitionAssignment(ctx context.Context, assignment *types.PartitionAssignment) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Deep copy.
	assignmentCopy := *assignment
	assignmentCopy.AssignedPartitions = make([]int, len(assignment.AssignedPartitions))
	copy(assignmentCopy.AssignedPartitions, assignment.AssignedPartitions)

	s.partitionAssignments[assignment.SchedulerID] = &assignmentCopy
	return nil
}

// LoadPartitionAssignment returns the stored partition assignment for a scheduler.
func (s *MemoryStorage) LoadPartitionAssignment(ctx context.Context, schedulerID int) (*types.PartitionAssignment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	assignment := s.partitionAssignments[schedulerID]
	if assignment == nil {
		return nil, nil
	}

	// Deep copy.
	assignmentCopy := *assignment
	assignmentCopy.AssignedPartitions = make([]int, len(assignment.AssignedPartitions))
	copy(assignmentCopy.AssignedPartitions, assignment.AssignedPartitions)

	return &assignmentCopy, nil
}

// SaveSyncState persists the sync state for a scheduler.
func (s *MemoryStorage) SaveSyncState(ctx context.Context, schedulerID int, state *SyncState) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Deep copy.
	stateCopy := &SyncState{
		SchedulerID:      state.SchedulerID,
		ConfigGeneration: state.ConfigGeneration,
		LastSyncTime:     make(map[int]int64),
		SyncGenerations:  make(map[int]int64),
	}
	for k, v := range state.LastSyncTime {
		stateCopy.LastSyncTime[k] = v
	}
	for k, v := range state.SyncGenerations {
		stateCopy.SyncGenerations[k] = v
	}

	s.syncStates[schedulerID] = stateCopy
	return nil
}

// LoadSyncState returns the stored sync state for a scheduler.
func (s *MemoryStorage) LoadSyncState(ctx context.Context, schedulerID int) (*SyncState, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	state := s.syncStates[schedulerID]
	if state == nil {
		return nil, nil
	}

	// Deep copy.
	stateCopy := &SyncState{
		SchedulerID:      state.SchedulerID,
		ConfigGeneration: state.ConfigGeneration,
		LastSyncTime:     make(map[int]int64),
		SyncGenerations:  make(map[int]int64),
	}
	for k, v := range state.LastSyncTime {
		stateCopy.LastSyncTime[k] = v
	}
	for k, v := range state.SyncGenerations {
		stateCopy.SyncGenerations[k] = v
	}

	return stateCopy, nil
}

// SaveGlobalCounts persists global counts.
func (s *MemoryStorage) SaveGlobalCounts(ctx context.Context, counts []int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.globalCounts = make([]int64, len(counts))
	copy(s.globalCounts, counts)
	return nil
}

// LoadGlobalCounts returns the stored global counts.
func (s *MemoryStorage) LoadGlobalCounts(ctx context.Context) ([]int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if s.globalCounts == nil {
		return nil, nil
	}

	counts := make([]int64, len(s.globalCounts))
	copy(counts, s.globalCounts)
	return counts, nil
}

// SavePartitionCounts persists per-partition counts.
func (s *MemoryStorage) SavePartitionCounts(ctx context.Context, partitionID int, counts []int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	countsCopy := make([]int64, len(counts))
	copy(countsCopy, counts)
	s.partitionCounts[partitionID] = countsCopy
	return nil
}

// LoadPartitionCounts returns the stored per-partition counts.
func (s *MemoryStorage) LoadPartitionCounts(ctx context.Context, partitionID int) ([]int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	counts := s.partitionCounts[partitionID]
	if counts == nil {
		return nil, nil
	}

	countsCopy := make([]int64, len(counts))
	copy(countsCopy, counts)
	return countsCopy, nil
}

// IncrementCount atomically increments a count.
func (s *MemoryStorage) IncrementCount(ctx context.Context, partitionID, rank int) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	counts := s.partitionCounts[partitionID]
	if counts == nil || rank >= len(counts) {
		return types.ErrPartitionNotFound
	}

	counts[rank]++
	return nil
}

// Reset clears all stored data.
func (s *MemoryStorage) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.adoptionStats = nil
	s.partitionAssignments = make(map[int]*types.PartitionAssignment)
	s.syncStates = make(map[int]*SyncState)
	s.globalCounts = nil
	s.partitionCounts = make(map[int][]int64)
}
