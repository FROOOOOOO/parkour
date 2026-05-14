package dispatcher

import (
	"testing"

	apisv1 "example.com/para-sched-api/apis/v1"
)

func makeSchedulers(names ...string) []*RegistrySchedulerInfo {
	schedulers := make([]*RegistrySchedulerInfo, len(names))
	for i, name := range names {
		schedulers[i] = &RegistrySchedulerInfo{
			Name:    name,
			ID:      i,
			Phase:   apisv1.SchedulerPhaseRunning,
			Healthy: true,
		}
	}
	return schedulers
}

func TestRebalance_MinimalMove_InitialAssignment(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1", "s2")
	numPartitions := 4

	result := r.Rebalance(schedulers, numPartitions, map[string][]int{})

	if !result.Changed {
		t.Error("expected Changed=true for initial assignment")
	}
	if len(result.Assignments) != 3 {
		t.Errorf("expected 3 assignments, got %d", len(result.Assignments))
	}
	// Each scheduler should get all partitions.
	for _, name := range []string{"s0", "s1", "s2"} {
		parts := result.Assignments[name]
		if len(parts) != numPartitions {
			t.Errorf("scheduler %s: got %d partitions, want %d", name, len(parts), numPartitions)
		}
	}
}

func TestRebalance_MinimalMove_NoChange(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1")
	numPartitions := 4

	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
	}

	result := r.Rebalance(schedulers, numPartitions, currentAssignments)

	if result.Changed {
		t.Error("expected Changed=false when nothing changed")
	}
}

func TestRebalance_MinimalMove_SchedulerAdded(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1", "s2")
	numPartitions := 4

	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
	}

	result := r.Rebalance(schedulers, numPartitions, currentAssignments)

	if !result.Changed {
		t.Error("expected Changed=true when scheduler added")
	}
	// New scheduler s2 should have all partitions.
	if len(result.Assignments["s2"]) != numPartitions {
		t.Errorf("s2 partitions: got %d, want %d", len(result.Assignments["s2"]), numPartitions)
	}
}

func TestRebalance_MinimalMove_SchedulerRemoved(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	// Only s0 and s2 are active (s1 left).
	schedulers := makeSchedulers("s0", "s2")
	numPartitions := 4

	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
		"s2": {0, 1, 2, 3},
	}

	result := r.Rebalance(schedulers, numPartitions, currentAssignments)

	if !result.Changed {
		t.Error("expected Changed=true when scheduler removed")
	}
	// s1 should not be in new assignments.
	if _, ok := result.Assignments["s1"]; ok {
		t.Error("removed scheduler s1 should not be in assignments")
	}
	// s0 and s2 should still have all partitions.
	for _, name := range []string{"s0", "s2"} {
		if len(result.Assignments[name]) != numPartitions {
			t.Errorf("%s: got %d partitions, want %d", name, len(result.Assignments[name]), numPartitions)
		}
	}
}

func TestRebalance_EvenDistribution(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyEvenDistribution)
	schedulers := makeSchedulers("s0", "s1")
	numPartitions := 4

	result := r.Rebalance(schedulers, numPartitions, map[string][]int{})

	if !result.Changed {
		t.Error("even distribution should always mark Changed=true")
	}
	for _, name := range []string{"s0", "s1"} {
		if len(result.Assignments[name]) != numPartitions {
			t.Errorf("%s: got %d partitions, want %d", name, len(result.Assignments[name]), numPartitions)
		}
	}
}

func TestRebalance_EvenDistribution_AlwaysChanged(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyEvenDistribution)
	schedulers := makeSchedulers("s0", "s1")
	numPartitions := 4
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
	}

	result := r.Rebalance(schedulers, numPartitions, currentAssignments)

	if !result.Changed {
		t.Error("even distribution should always report Changed=true")
	}
}

func TestNeedsRebalance_SchedulerCountChanged(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1", "s2")
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
	}

	if !r.NeedsRebalance(schedulers, 4, currentAssignments, 2) {
		t.Error("should need rebalance when scheduler count changed")
	}
}

func TestNeedsRebalance_MissingScheduler(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1")
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s2": {0, 1, 2, 3}, // s2 is assigned but not active
	}

	if !r.NeedsRebalance(schedulers, 4, currentAssignments, 2) {
		t.Error("should need rebalance when active scheduler missing from assignments")
	}
}

func TestNeedsRebalance_Balanced(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1")
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3},
	}

	if r.NeedsRebalance(schedulers, 4, currentAssignments, 2) {
		t.Error("should not need rebalance when balanced")
	}
}

func TestNeedsRebalance_StaleAssignment(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0")
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
		"s1": {0, 1, 2, 3}, // s1 no longer active
	}

	if !r.NeedsRebalance(schedulers, 4, currentAssignments, 2) {
		t.Error("should need rebalance when stale scheduler in assignments")
	}
}

func TestRebalance_Moves(t *testing.T) {
	r := NewRebalancer(apisv1.RebalancePolicyMinimalMove)
	schedulers := makeSchedulers("s0", "s1")
	numPartitions := 4

	// s0 already assigned, s1 is new.
	currentAssignments := map[string][]int{
		"s0": {0, 1, 2, 3},
	}

	result := r.Rebalance(schedulers, numPartitions, currentAssignments)
	if !result.Changed {
		t.Fatal("expected Changed=true")
	}
	// Should have moves for s1 gaining all partitions.
	foundMoves := 0
	for _, m := range result.Moves {
		if m.ToScheduler == "s1" {
			foundMoves++
		}
	}
	if foundMoves != numPartitions {
		t.Errorf("expected %d moves to s1, got %d", numPartitions, foundMoves)
	}
}

func TestMakeFullPartitionList(t *testing.T) {
	list := makeFullPartitionList(5)
	if len(list) != 5 {
		t.Fatalf("len: got %d, want 5", len(list))
	}
	for i, v := range list {
		if v != i {
			t.Errorf("list[%d]=%d, want %d", i, v, i)
		}
	}
}
