package stats

import (
	"testing"
	"time"

	"example.com/scheduler-lib/types"
)

func TestAdoptionStatsCache_UpdateResult(t *testing.T) {
	cache := NewAdoptionStatsCache(DefaultStatsConfig())

	// Simulate a series of binding results.  Note the event classification
	// in UpdateResult (see adoption_cache.go):
	//   - Success=true,  NodeName=X  → pod success (pod-level; recorded in global/partition/node)
	//   - Success=false, NodeName="" → ACF all-failed (pod-level; recorded in global/partition)
	//   - Success=false, NodeName=X  → candidate-level failure (node only; not in global)
	// This test focuses on the pod-level path; the candidate-level path is
	// covered by TestAdoptionStatsCache_CandidateLevelFailureIsolated.
	results := []types.BindingResult{
		{Success: true, Rank: 0, NodeName: "node-1", PartitionID: 0},
		{Success: true, Rank: 0, NodeName: "node-1", PartitionID: 0},
		{Success: true, Rank: 1, NodeName: "node-2", PartitionID: 0},
		{Success: true, Rank: 0, NodeName: "node-1", PartitionID: 1},
		{Success: false, Rank: -1, NodeName: "", PartitionID: 0}, // ACF
	}

	for _, r := range results {
		r.Timestamp = time.Now()
		cache.UpdateResult(r)
	}

	stats := cache.GetStats()

	// Verify total count.
	if stats.TotalBindings != 5 {
		t.Errorf("expected 5 total bindings, got %d", stats.TotalBindings)
	}

	// Verify success/failure counts.
	if stats.SuccessCount != 4 {
		t.Errorf("expected 4 successes, got %d", stats.SuccessCount)
	}
	if stats.FailureCount != 1 {
		t.Errorf("expected 1 failure, got %d", stats.FailureCount)
	}

	// Verify rank distribution.
	counts := cache.GetGlobalCounts()
	if counts[0] != 3 { // rank 0 adopted 3 times
		t.Errorf("expected rank 0 count = 3, got %d", counts[0])
	}
	if counts[1] != 1 { // rank 1 adopted 1 time
		t.Errorf("expected rank 1 count = 1, got %d", counts[1])
	}
	// ACF events are recorded in the last slot of globalCounts.
	if last := counts[len(counts)-1]; last != 1 {
		t.Errorf("expected ACF count (last slot) = 1, got %d", last)
	}
}

// TestAdoptionStatsCache_CandidateLevelFailureIsolated verifies that a
// candidate-level failure (Success=false with a non-empty NodeName) only
// updates nodeStats and does not leak into the pod-level TotalBindings or
// FailureCount.  This isolation prevents the double-counting bug where one ACF
// pod would be recorded as K+1 failures + 1 ACF simultaneously.
func TestAdoptionStatsCache_CandidateLevelFailureIsolated(t *testing.T) {
	cache := NewAdoptionStatsCache(DefaultStatsConfig())

	// One candidate-level failure: node-X's individual bind call failed, but
	// the pod as a whole may still succeed via another candidate.
	cache.UpdateResult(types.BindingResult{
		Success: false, Rank: -1, NodeName: "node-X", PartitionID: 0,
		Timestamp: time.Now(),
	})

	stats := cache.GetStats()
	if stats.TotalBindings != 0 {
		t.Errorf("candidate-level failure leaked into TotalBindings: got %d, want 0", stats.TotalBindings)
	}
	if stats.FailureCount != 0 {
		t.Errorf("candidate-level failure leaked into FailureCount: got %d, want 0", stats.FailureCount)
	}

	// The node dimension should record the conflict.
	if cr := cache.GetNodeConflictRate("node-X"); cr != 1.0 {
		t.Errorf("node-X conflict rate: got %f, want 1.0", cr)
	}
}

// TestAdoptionStatsCache_GetNodeConflictRate verifies the cold-start,
// partial-sample, and all-failure scenarios for node conflict rate.
func TestAdoptionStatsCache_GetNodeConflictRate(t *testing.T) {
	cache := NewAdoptionStatsCache(DefaultStatsConfig())

	// Cold-start: an unknown node returns 0 (prevents Penalty from firing on
	// spurious data at startup).
	if cr := cache.GetNodeConflictRate("unknown"); cr != 0 {
		t.Errorf("cold start: expected 0, got %f", cr)
	}

	// All successes: conflict rate should be 0.
	for i := 0; i < 20; i++ {
		cache.UpdateResult(types.BindingResult{
			Success: true, Rank: 0, NodeName: "node-ok", Timestamp: time.Now(),
		})
	}
	if cr := cache.GetNodeConflictRate("node-ok"); cr != 0 {
		t.Errorf("all success: expected 0, got %f", cr)
	}

	// All failures: conflict rate should be 1.
	for i := 0; i < 20; i++ {
		cache.UpdateResult(types.BindingResult{
			Success: false, Rank: -1, NodeName: "node-bad", Timestamp: time.Now(),
		})
	}
	if cr := cache.GetNodeConflictRate("node-bad"); cr != 1.0 {
		t.Errorf("all failure: expected 1.0, got %f", cr)
	}

	// Mixed: 10 successes + 10 failures → conflict rate ≈ 0.5.
	for i := 0; i < 10; i++ {
		cache.UpdateResult(types.BindingResult{
			Success: true, Rank: 0, NodeName: "node-mix", Timestamp: time.Now(),
		})
		cache.UpdateResult(types.BindingResult{
			Success: false, Rank: -1, NodeName: "node-mix", Timestamp: time.Now(),
		})
	}
	if cr := cache.GetNodeConflictRate("node-mix"); cr < 0.45 || cr > 0.55 {
		t.Errorf("mixed: expected ~0.5, got %f", cr)
	}
}

func TestSlidingWindow(t *testing.T) {
	window := NewSlidingWindow(10)

	// Add 5 successes and 5 failures.
	for i := 0; i < 5; i++ {
		window.Add(true)
		window.Add(false)
	}

	rate := window.SuccessRate()
	if rate != 0.5 {
		t.Errorf("expected success rate 0.5, got %.2f", rate)
	}

	// Add more successes; old data should be evicted.
	for i := 0; i < 10; i++ {
		window.Add(true)
	}

	rate2 := window.SuccessRate()
	// Window size is 10 and all entries are now successes.
	if rate2 != 1.0 {
		t.Errorf("expected success rate 1.0, got %.2f", rate2)
	}
}

func BenchmarkAdoptionStatsCache_UpdateResult(b *testing.B) {
	cache := NewAdoptionStatsCache(DefaultStatsConfig())
	result := types.BindingResult{
		Success:     true,
		Rank:        0,
		NodeName:    "node-1",
		PartitionID: 0,
		Timestamp:   time.Now(),
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cache.UpdateResult(result)
	}
}

func BenchmarkAdoptionStatsCache_GetNodeConflictRate(b *testing.B) {
	cache := NewAdoptionStatsCache(DefaultStatsConfig())

	// Pre-populate data.
	for i := 0; i < 1000; i++ {
		cache.UpdateResult(types.BindingResult{
			Success:     i%3 != 0,
			Rank:        i % 3,
			NodeName:    "node-1",
			PartitionID: i % 4,
			Timestamp:   time.Now(),
		})
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cache.GetNodeConflictRate("node-1")
	}
}
