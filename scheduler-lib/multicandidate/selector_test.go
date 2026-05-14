package multicandidate

import (
	"fmt"
	"testing"

	"example.com/scheduler-lib/types"
)

// mockAdapter is a mock scheduler adapter for testing.
type mockAdapter struct {
	scores       []types.NodeScore
	clusterSize  int
	schedulerID  int
	highPriority bool
	podKey       string
}

func (m *mockAdapter) GetNodeScores() []types.NodeScore { return m.scores }
func (m *mockAdapter) GetClusterSize() int              { return m.clusterSize }
func (m *mockAdapter) GetSchedulerID() int              { return m.schedulerID }
func (m *mockAdapter) IsHighPriorityPod() bool          { return m.highPriority }
func (m *mockAdapter) GetPodKey() string                { return m.podKey }

func TestCandidateSelector_SelectCandidates(t *testing.T) {
	config := DefaultConfig()
	config.DefaultK = 3
	config.ScoreThreshold = 0.8

	selector, err := NewCandidateSelector(config)
	if err != nil {
		t.Fatalf("failed to create selector: %v", err)
	}

	adapter := &mockAdapter{
		scores: []types.NodeScore{
			{NodeName: "node-1", Score: 100},
			{NodeName: "node-2", Score: 95},
			{NodeName: "node-3", Score: 90},
			{NodeName: "node-4", Score: 85},
			{NodeName: "node-5", Score: 70}, // below threshold
		},
		clusterSize: 100,
		podKey:      "default/test-pod",
	}

	result, err := selector.SelectCandidates(adapter)
	if err != nil {
		t.Fatalf("SelectCandidates failed: %v", err)
	}

	// Verify candidate count.
	if len(result.Candidates) != 3 {
		t.Errorf("expected 3 candidates, got %d", len(result.Candidates))
	}

	// Verify the primary candidate is the highest-scoring node.
	if result.GetPrimary().NodeName != "node-1" {
		t.Errorf("expected primary to be node-1, got %s", result.GetPrimary().NodeName)
	}

	// Verify node-5 is filtered out (below the 80% threshold).
	for _, c := range result.Candidates {
		if c.NodeName == "node-5" {
			t.Error("node-5 should be filtered out")
		}
	}
}

func TestCandidateSelector_DynamicK(t *testing.T) {
	config := DefaultConfig()
	config.DefaultK = 3
	config.EnableDynamicK = true
	config.HighPriorityExtraK = 2
	config.ScoreThreshold = 0.8

	selector, _ := NewCandidateSelector(config)

	// Normal pod.
	normalAdapter := &mockAdapter{
		scores:       generateScores(10),
		clusterSize:  100,
		highPriority: false,
	}

	result1, _ := selector.SelectCandidates(normalAdapter)
	if len(result1.Candidates) != 3 {
		t.Errorf("normal pod: expected 3 candidates, got %d", len(result1.Candidates))
	}

	// High-priority pod.
	highPriorityAdapter := &mockAdapter{
		scores:       generateScores(10),
		clusterSize:  100,
		highPriority: true,
	}

	result2, _ := selector.SelectCandidates(highPriorityAdapter)
	if len(result2.Candidates) != 5 { // 3 + 2
		t.Errorf("high priority pod: expected 5 candidates, got %d", len(result2.Candidates))
	}
}

func TestCandidateSelector_ScoreThreshold(t *testing.T) {
	config := DefaultConfig()
	config.DefaultK = 5
	config.ScoreThreshold = 0.9 // strict threshold

	selector, _ := NewCandidateSelector(config)

	adapter := &mockAdapter{
		scores: []types.NodeScore{
			{NodeName: "node-1", Score: 100},
			{NodeName: "node-2", Score: 95},
			{NodeName: "node-3", Score: 90},
			{NodeName: "node-4", Score: 85}, // below 90
			{NodeName: "node-5", Score: 80},
		},
		clusterSize: 100,
	}

	result, _ := selector.SelectCandidates(adapter)

	// Only nodes above 90 (scores 100, 95, 90).
	if len(result.Candidates) != 3 {
		t.Errorf("expected 3 candidates (above 90%% threshold), got %d", len(result.Candidates))
	}
}

func TestSelectionStrategy_QualityFirst(t *testing.T) {
	// Paper Penalty formula: adjusted = (1-p)*normScore + p*(1-conflictRate)
	// p=0.3, maxScore=100, manual calculation:
	//   node-1: (1-0.3)*1.00 + 0.3*(1-0.8) = 0.70 + 0.06 = 0.76
	//   node-2: (1-0.3)*0.95 + 0.3*(1-0.1) = 0.665 + 0.27 = 0.935  ← primary
	//   node-3: (1-0.3)*0.90 + 0.3*(1-0.5) = 0.630 + 0.15 = 0.780
	strategy := NewQualityFirstStrategy(0.3)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "node-1", Score: 100}, ConflictRate: 0.8},
		{NodeScore: types.NodeScore{NodeName: "node-2", Score: 95}, ConflictRate: 0.1},
		{NodeScore: types.NodeScore{NodeName: "node-3", Score: 90}, ConflictRate: 0.5},
	}

	selected := strategy.Select(candidates, 2)

	if len(selected) != 2 {
		t.Errorf("expected 2 candidates, got %d", len(selected))
	}

	// node-2 (combined score 0.935) should be primary.
	if selected[0].NodeName != "node-2" {
		t.Errorf("expected node-2 as primary, got %s", selected[0].NodeName)
	}
	// node-3 (0.780) should be secondary.
	if selected[1].NodeName != "node-3" {
		t.Errorf("expected node-3 as secondary, got %s", selected[1].NodeName)
	}
}

// TestSelectionStrategy_QualityFirst_NoPenalty verifies that p=0 degrades to
// pure Score ordering.
func TestSelectionStrategy_QualityFirst_NoPenalty(t *testing.T) {
	strategy := NewQualityFirstStrategy(0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "node-1", Score: 100}, ConflictRate: 0.9},
		{NodeScore: types.NodeScore{NodeName: "node-2", Score: 95}, ConflictRate: 0.0},
		{NodeScore: types.NodeScore{NodeName: "node-3", Score: 90}, ConflictRate: 0.0},
	}

	selected := strategy.Select(candidates, 3)

	// With p=0 conflictRate is completely ignored; ordering is by Score: node-1 > node-2 > node-3.
	if selected[0].NodeName != "node-1" {
		t.Errorf("p=0: expected node-1 as primary (highest score), got %s", selected[0].NodeName)
	}
	if selected[1].NodeName != "node-2" {
		t.Errorf("p=0: expected node-2 as secondary, got %s", selected[1].NodeName)
	}
}

// TestSelectionStrategy_QualityFirst_FullPenalty verifies that p=1 orders
// purely by (1-conflictRate).
func TestSelectionStrategy_QualityFirst_FullPenalty(t *testing.T) {
	strategy := NewQualityFirstStrategy(1.0)

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "node-1", Score: 100}, ConflictRate: 0.8}, // 1-0.8 = 0.2
		{NodeScore: types.NodeScore{NodeName: "node-2", Score: 50}, ConflictRate: 0.0},  // 1-0.0 = 1.0
		{NodeScore: types.NodeScore{NodeName: "node-3", Score: 80}, ConflictRate: 0.4},  // 1-0.4 = 0.6
	}

	selected := strategy.Select(candidates, 3)

	// With p=1 Score is ignored; ordering by (1-conflictRate): node-2 > node-3 > node-1.
	if selected[0].NodeName != "node-2" {
		t.Errorf("p=1: expected node-2 as primary (lowest conflict), got %s", selected[0].NodeName)
	}
	if selected[1].NodeName != "node-3" {
		t.Errorf("p=1: expected node-3 as secondary, got %s", selected[1].NodeName)
	}
}

func TestSelectionStrategy_LatencyFirst(t *testing.T) {
	strategy := NewLatencyFirstStrategy()

	candidates := []types.CandidateNode{
		{NodeScore: types.NodeScore{NodeName: "node-1", Score: 100}, Freshness: 0.5},  // stale
		{NodeScore: types.NodeScore{NodeName: "node-2", Score: 90}, Freshness: 0.95}, // fresh
		{NodeScore: types.NodeScore{NodeName: "node-3", Score: 80}, Freshness: 0.9},  // fresh
	}

	selected := strategy.Select(candidates, 2)

	// Fresh nodes should be preferred.
	if selected[0].NodeName != "node-2" {
		t.Errorf("expected node-2 (fresh) as primary, got %s", selected[0].NodeName)
	}
}

func generateScores(n int) []types.NodeScore {
	scores := make([]types.NodeScore, n)
	for i := 0; i < n; i++ {
		scores[i] = types.NodeScore{
			NodeName: fmt.Sprintf("node-%d", i+1),
			Score:    int64(100 - i*5),
		}
	}
	return scores
}

// Benchmark
func BenchmarkCandidateSelector_SelectCandidates(b *testing.B) {
	config := DefaultConfig()
	selector, _ := NewCandidateSelector(config)

	adapter := &mockAdapter{
		scores:      generateScores(100),
		clusterSize: 10000,
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		selector.SelectCandidates(adapter)
	}
}
