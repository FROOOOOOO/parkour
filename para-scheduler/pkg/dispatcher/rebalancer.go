package dispatcher

import (
	"sort"

	apisv1 "example.com/para-sched-api/apis/v1"
)

// PartitionMove records a single partition migration between schedulers.
type PartitionMove struct {
	PartitionID   int
	FromScheduler string
	ToScheduler   string
}

// RebalanceResult holds the outcome of a rebalance operation.
type RebalanceResult struct {
	Assignments map[string][]int // schedulerName -> assigned partitions
	Changed     bool             // whether any assignment changed
	Moves       []PartitionMove  // detailed change log
}

// Rebalancer computes partition re-assignments when the set of active schedulers changes.
type Rebalancer struct {
	policy apisv1.RebalancePolicy
}

// NewRebalancer creates a Rebalancer with the given policy.
func NewRebalancer(policy apisv1.RebalancePolicy) *Rebalancer {
	return &Rebalancer{policy: policy}
}

// Rebalance computes new partition assignments based on the active schedulers.
//
// In the current ParSync architecture, all schedulers own all partitions —
// the difference is only in SyncSlot offsets. So "rebalance" here means:
//   - Detect whether the scheduler set (N) or ID mapping changed
//   - If changed, produce an updated Assignments map and mark Changed=true
//   - The Coordinator then uses this to recalculate SyncSlots via ClockManager
func (r *Rebalancer) Rebalance(
	schedulers []*RegistrySchedulerInfo,
	numPartitions int,
	currentAssignments map[string][]int,
) *RebalanceResult {
	switch r.policy {
	case apisv1.RebalancePolicyEvenDistribution:
		return r.rebalanceEvenDistribution(schedulers, numPartitions)
	default: // MinimalMove
		return r.rebalanceMinimalMove(schedulers, numPartitions, currentAssignments)
	}
}

// NeedsRebalance checks whether the current assignment deviates beyond the given threshold.
// In the "all schedulers own all partitions" model, this checks whether the scheduler
// count has changed or any scheduler's partition list differs from the full set.
func (r *Rebalancer) NeedsRebalance(
	schedulers []*RegistrySchedulerInfo,
	numPartitions int,
	currentAssignments map[string][]int,
	threshold int,
) bool {
	// Check if scheduler count changed.
	if len(schedulers) != len(currentAssignments) {
		return true
	}

	allPartitions := makeFullPartitionList(numPartitions)

	// Check if any active scheduler is missing from current or has wrong partition count.
	for _, s := range schedulers {
		assigned, ok := currentAssignments[s.Name]
		if !ok {
			return true
		}
		diff := len(allPartitions) - len(assigned)
		if diff < 0 {
			diff = -diff
		}
		if diff > threshold {
			return true
		}
	}

	// Check if any current assignment belongs to a scheduler that's no longer active.
	activeNames := make(map[string]bool, len(schedulers))
	for _, s := range schedulers {
		activeNames[s.Name] = true
	}
	for name := range currentAssignments {
		if !activeNames[name] {
			return true
		}
	}

	return false
}

// rebalanceMinimalMove preserves existing assignments where possible,
// only updating schedulers that joined, left, or have incomplete partition sets.
func (r *Rebalancer) rebalanceMinimalMove(
	schedulers []*RegistrySchedulerInfo,
	numPartitions int,
	currentAssignments map[string][]int,
) *RebalanceResult {
	result := &RebalanceResult{
		Assignments: make(map[string][]int, len(schedulers)),
	}

	allPartitions := makeFullPartitionList(numPartitions)

	// Build set of active scheduler names.
	activeNames := make(map[string]bool, len(schedulers))
	for _, s := range schedulers {
		activeNames[s.Name] = true
	}

	// Detect removed schedulers.
	for name := range currentAssignments {
		if !activeNames[name] {
			result.Changed = true
			for _, pid := range currentAssignments[name] {
				result.Moves = append(result.Moves, PartitionMove{
					PartitionID:   pid,
					FromScheduler: name,
					ToScheduler:   "", // will be reassigned
				})
			}
		}
	}

	// For each active scheduler, assign all partitions (ParSync model).
	for _, s := range schedulers {
		prev, hadPrev := currentAssignments[s.Name]
		result.Assignments[s.Name] = make([]int, len(allPartitions))
		copy(result.Assignments[s.Name], allPartitions)

		if !hadPrev {
			// New scheduler.
			result.Changed = true
			for _, pid := range allPartitions {
				result.Moves = append(result.Moves, PartitionMove{
					PartitionID:   pid,
					FromScheduler: "",
					ToScheduler:   s.Name,
				})
			}
		} else if len(prev) != len(allPartitions) {
			// Partition count changed.
			result.Changed = true
		}
	}

	return result
}

// rebalanceEvenDistribution ignores existing assignments and builds from scratch.
func (r *Rebalancer) rebalanceEvenDistribution(
	schedulers []*RegistrySchedulerInfo,
	numPartitions int,
) *RebalanceResult {
	result := &RebalanceResult{
		Assignments: make(map[string][]int, len(schedulers)),
		Changed:     true, // always changed in even distribution
	}

	allPartitions := makeFullPartitionList(numPartitions)
	for _, s := range schedulers {
		result.Assignments[s.Name] = make([]int, len(allPartitions))
		copy(result.Assignments[s.Name], allPartitions)
	}

	return result
}

// makeFullPartitionList returns [0, 1, ..., n-1].
func makeFullPartitionList(n int) []int {
	parts := make([]int, n)
	for i := 0; i < n; i++ {
		parts[i] = i
	}
	return parts
}

// sortedSchedulerNames returns scheduler names sorted for deterministic output.
func sortedSchedulerNames(schedulers []*RegistrySchedulerInfo) []string {
	names := make([]string, len(schedulers))
	for i, s := range schedulers {
		names[i] = s.Name
	}
	sort.Strings(names)
	return names
}
