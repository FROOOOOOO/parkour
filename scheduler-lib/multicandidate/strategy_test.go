package multicandidate

import (
	"testing"

	"example.com/scheduler-lib/types"
)

// TestLatencyFirst_RankAssigned verifies that LatencyFirst correctly assigns
// the Rank field after re-sorting.  Before Phase 2 the implementation sorted
// but did not set Rank, causing the upper integration layer to read the
// pre-sort rank values.
func TestLatencyFirst_RankAssigned(t *testing.T) {
	strategy := NewLatencyFirstStrategy()

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "a", Score: 100}, Freshness: 0.1, Rank: 99},
		{NodeScore: types.NodeScore{NodeName: "b", Score: 80}, Freshness: 0.9, Rank: 99},
		{NodeScore: types.NodeScore{NodeName: "c", Score: 90}, Freshness: 0.5, Rank: 99},
	}
	selected := strategy.Select(candidates, 3)

	// After sorting: b(0.9) > c(0.5) > a(0.1)
	wantOrder := []string{"b", "c", "a"}
	for i, w := range wantOrder {
		if selected[i].NodeName != w {
			t.Errorf("rank %d: got %s, want %s", i, selected[i].NodeName, w)
		}
		if selected[i].Rank != i {
			t.Errorf("rank %d: Rank field = %d, want %d", i, selected[i].Rank, i)
		}
	}
}

// TestLatencyFirst_TieBreakByScore verifies that equal Freshness values are
// broken by Score descending.
func TestLatencyFirst_TieBreakByScore(t *testing.T) {
	strategy := NewLatencyFirstStrategy()

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "a", Score: 60}, Freshness: 0.9},
		{NodeScore: types.NodeScore{NodeName: "b", Score: 100}, Freshness: 0.9},
		{NodeScore: types.NodeScore{NodeName: "c", Score: 80}, Freshness: 0.9},
	}
	selected := strategy.Select(candidates, 3)

	// Equal Freshness → sort by Score descending: b(100) > c(80) > a(60)
	wantOrder := []string{"b", "c", "a"}
	for i, w := range wantOrder {
		if selected[i].NodeName != w {
			t.Errorf("rank %d: got %s, want %s", i, selected[i].NodeName, w)
		}
	}
}

// TestWeightedRandom_Determinism verifies that with a fixed seed, multiple runs
// produce the same ordering (deterministic and reproducible).
func TestWeightedRandom_Determinism(t *testing.T) {
	makeCandidates := func() []types.CandidateNode {
		return []types.CandidateNode{
			{NodeScore: types.NodeScore{NodeName: "a", Score: 100}, ConflictRate: 0.0},
			{NodeScore: types.NodeScore{NodeName: "b", Score: 80}, ConflictRate: 0.5},
			{NodeScore: types.NodeScore{NodeName: "c", Score: 60}, ConflictRate: 0.2},
			{NodeScore: types.NodeScore{NodeName: "d", Score: 40}, ConflictRate: 0.8},
		}
	}

	const seed = int64(42)
	s1 := NewWeightedRandomStrategy(seed, 0.3)
	out1 := s1.Select(makeCandidates(), 3)

	s2 := NewWeightedRandomStrategy(seed, 0.3)
	out2 := s2.Select(makeCandidates(), 3)

	if len(out1) != len(out2) {
		t.Fatalf("len: got %d vs %d", len(out1), len(out2))
	}
	for i := range out1 {
		if out1[i].NodeName != out2[i].NodeName {
			t.Errorf("at %d: %s vs %s", i, out1[i].NodeName, out2[i].NodeName)
		}
		if out1[i].Rank != i || out2[i].Rank != i {
			t.Errorf("Rank at %d: %d / %d, want %d", i, out1[i].Rank, out2[i].Rank, i)
		}
	}
}

// TestWeightedRandom_AllZeroScores verifies that totalWeight==0 degrades to
// uniform sampling and still returns K candidates.
func TestWeightedRandom_AllZeroScores(t *testing.T) {
	s := NewWeightedRandomStrategy(42, 0.0) // p=0, score=0 -> totalWeight=0

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "a", Score: 0}},
		{NodeScore: types.NodeScore{NodeName: "b", Score: 0}},
		{NodeScore: types.NodeScore{NodeName: "c", Score: 0}},
	}
	selected := s.Select(candidates, 2)
	if len(selected) != 2 {
		t.Fatalf("len: got %d, want 2", len(selected))
	}
	seen := map[string]bool{}
	for _, c := range selected {
		if seen[c.NodeName] {
			t.Errorf("duplicate selection: %s", c.NodeName)
		}
		seen[c.NodeName] = true
	}
}

// TestWeightedRandom_PoolSmallerThanK verifies that when the pool is smaller
// than k, all candidates are returned in original order with Rank set.
func TestWeightedRandom_PoolSmallerThanK(t *testing.T) {
	s := NewWeightedRandomStrategy(42, 0.3)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "a", Score: 100}},
		{NodeScore: types.NodeScore{NodeName: "b", Score: 80}},
	}
	selected := s.Select(candidates, 5)
	if len(selected) != 2 {
		t.Fatalf("len: got %d, want 2", len(selected))
	}
	if selected[0].Rank != 0 || selected[1].Rank != 1 {
		t.Errorf("ranks: got %d/%d", selected[0].Rank, selected[1].Rank)
	}
}

// TestQualityFirstParSync_PicksHighestAvgPartition verifies that QF-ParSync
// selects the partition with the highest average score first.
// Partition P1 average=90, P0 average=50; with K=2 both candidates should come from P1.
func TestQualityFirstParSync_PicksHighestAvgPartition(t *testing.T) {
	s := NewQualityFirstParSyncStrategy(42, 0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "p0a", Score: 60}, PartitionID: 0},
		{NodeScore: types.NodeScore{NodeName: "p0b", Score: 40}, PartitionID: 0},
		{NodeScore: types.NodeScore{NodeName: "p1a", Score: 100}, PartitionID: 1},
		{NodeScore: types.NodeScore{NodeName: "p1b", Score: 80}, PartitionID: 1},
	}
	selected := s.Select(candidates, 2)
	if len(selected) != 2 {
		t.Fatalf("len: got %d, want 2", len(selected))
	}
	for i, c := range selected {
		if c.PartitionID != 1 {
			t.Errorf("rank %d (%s): PartitionID=%d, want 1 (highest-avg partition)", i, c.NodeName, c.PartitionID)
		}
		if c.Rank != i {
			t.Errorf("rank %d: Rank field=%d", i, c.Rank)
		}
	}
}

// TestQualityFirstParSync_FallsBackWhenInsufficient verifies that when K
// exceeds the best partition's size, QF-ParSync falls back to the next best.
// P1 size=1, P0 size=2; K=3 should fill 1 from P1 + 2 from P0.
func TestQualityFirstParSync_FallsBackWhenInsufficient(t *testing.T) {
	s := NewQualityFirstParSyncStrategy(42, 0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "p0a", Score: 60}, PartitionID: 0},
		{NodeScore: types.NodeScore{NodeName: "p0b", Score: 50}, PartitionID: 0},
		{NodeScore: types.NodeScore{NodeName: "p1a", Score: 100}, PartitionID: 1},
	}
	selected := s.Select(candidates, 3)
	if len(selected) != 3 {
		t.Fatalf("len: got %d, want 3", len(selected))
	}
	// Rank 0 must come from P1 (best partition); ranks 1 and 2 come from P0 (fallback).
	if selected[0].PartitionID != 1 {
		t.Errorf("primary: PartitionID=%d, want 1", selected[0].PartitionID)
	}
	for i := 1; i < 3; i++ {
		if selected[i].PartitionID != 0 {
			t.Errorf("rank %d: PartitionID=%d, want 0 (fallback partition)", i, selected[i].PartitionID)
		}
	}
}

// TestLatencyFirstParSync_PicksFreshestPartition verifies that LF-ParSync
// prefers the freshest partition.
// P0 freshness=0.2, P1 freshness=0.9; K=2 should come entirely from P1.
func TestLatencyFirstParSync_PicksFreshestPartition(t *testing.T) {
	s := NewLatencyFirstParSyncStrategy(42, 0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "p0a", Score: 100}, PartitionID: 0, Freshness: 0.2},
		{NodeScore: types.NodeScore{NodeName: "p0b", Score: 90}, PartitionID: 0, Freshness: 0.2},
		{NodeScore: types.NodeScore{NodeName: "p1a", Score: 60}, PartitionID: 1, Freshness: 0.9},
		{NodeScore: types.NodeScore{NodeName: "p1b", Score: 50}, PartitionID: 1, Freshness: 0.9},
	}
	selected := s.Select(candidates, 2)
	if len(selected) != 2 {
		t.Fatalf("len: got %d, want 2", len(selected))
	}
	for i, c := range selected {
		if c.PartitionID != 1 {
			t.Errorf("rank %d (%s): PartitionID=%d, want 1 (freshest)", i, c.NodeName, c.PartitionID)
		}
	}
}

// TestLatencyFirstParSync_FallBackOrder verifies that when the freshest
// partition is insufficient, fallback follows descending freshness order.
// freshness: P2=0.9, P1=0.5, P0=0.1; K=3, one node per partition → P2→P1→P0.
func TestLatencyFirstParSync_FallBackOrder(t *testing.T) {
	s := NewLatencyFirstParSyncStrategy(42, 0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "p0", Score: 100}, PartitionID: 0, Freshness: 0.1},
		{NodeScore: types.NodeScore{NodeName: "p1", Score: 80}, PartitionID: 1, Freshness: 0.5},
		{NodeScore: types.NodeScore{NodeName: "p2", Score: 60}, PartitionID: 2, Freshness: 0.9},
	}
	selected := s.Select(candidates, 3)
	wantOrder := []string{"p2", "p1", "p0"}
	for i, w := range wantOrder {
		if selected[i].NodeName != w {
			t.Errorf("rank %d: got %s, want %s", i, selected[i].NodeName, w)
		}
	}
}

// TestParSync_Determinism verifies that with a fixed seed, multiple runs
// produce the same ordering.
func TestParSync_Determinism(t *testing.T) {
	makeCandidates := func() []types.CandidateNode {
		return []types.CandidateNode{
			{NodeScore: types.NodeScore{NodeName: "p0a", Score: 60}, PartitionID: 0, ConflictRate: 0.1},
			{NodeScore: types.NodeScore{NodeName: "p0b", Score: 50}, PartitionID: 0, ConflictRate: 0.2},
			{NodeScore: types.NodeScore{NodeName: "p1a", Score: 100}, PartitionID: 1, ConflictRate: 0.0},
			{NodeScore: types.NodeScore{NodeName: "p1b", Score: 80}, PartitionID: 1, ConflictRate: 0.3},
		}
	}
	const seed = int64(7)

	s1 := NewQualityFirstParSyncStrategy(seed, 0.3)
	s2 := NewQualityFirstParSyncStrategy(seed, 0.3)
	out1 := s1.Select(makeCandidates(), 3)
	out2 := s2.Select(makeCandidates(), 3)
	if len(out1) != len(out2) {
		t.Fatalf("len: %d vs %d", len(out1), len(out2))
	}
	for i := range out1 {
		if out1[i].NodeName != out2[i].NodeName {
			t.Errorf("at %d: %s vs %s", i, out1[i].NodeName, out2[i].NodeName)
		}
	}
}

// TestParSync_PenaltyOverlay verifies that with p>0, high conflict-rate nodes
// are penalised: P0 nodes have high score but cr=0.95; P1 nodes have medium
// score but cr=0; with p=1 only (1-cr) matters, so P1 should be selected.
func TestParSync_PenaltyOverlay(t *testing.T) {
	s := NewQualityFirstParSyncStrategy(42, 1.0) // p=1 → orders purely by (1-cr)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "p0a", Score: 100}, PartitionID: 0, ConflictRate: 0.95},
		{NodeScore: types.NodeScore{NodeName: "p0b", Score: 100}, PartitionID: 0, ConflictRate: 0.95},
		{NodeScore: types.NodeScore{NodeName: "p1a", Score: 50}, PartitionID: 1, ConflictRate: 0.0},
		{NodeScore: types.NodeScore{NodeName: "p1b", Score: 50}, PartitionID: 1, ConflictRate: 0.0},
	}
	selected := s.Select(candidates, 2)
	for i, c := range selected {
		if c.PartitionID != 1 {
			t.Errorf("rank %d (%s): PartitionID=%d, want 1 (penalty pushed P0 below)", i, c.NodeName, c.PartitionID)
		}
	}
}

// TestParSync_SinglePartition verifies that with a single partition the
// strategy degrades to weighted intra-partition sampling.
func TestParSync_SinglePartition(t *testing.T) {
	s := NewLatencyFirstParSyncStrategy(42, 0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "a", Score: 100}, PartitionID: 0, Freshness: 1.0},
		{NodeScore: types.NodeScore{NodeName: "b", Score: 80}, PartitionID: 0, Freshness: 1.0},
		{NodeScore: types.NodeScore{NodeName: "c", Score: 60}, PartitionID: 0, Freshness: 1.0},
	}
	selected := s.Select(candidates, 2)
	if len(selected) != 2 {
		t.Fatalf("len: got %d, want 2", len(selected))
	}
	seen := map[string]bool{}
	for _, c := range selected {
		if seen[c.NodeName] {
			t.Errorf("duplicate: %s", c.NodeName)
		}
		seen[c.NodeName] = true
	}
}
