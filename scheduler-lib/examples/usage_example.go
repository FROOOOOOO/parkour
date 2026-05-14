package main

import (
	"fmt"
	"time"

	"example.com/scheduler-lib/multicandidate"
	"example.com/scheduler-lib/parsync"
	"example.com/scheduler-lib/stats"
	"example.com/scheduler-lib/types"
)

func main() {
	// 1. Create components.

	// Stats cache.
	statsCache := stats.NewAdoptionStatsCache(stats.DefaultStatsConfig())

	// Partition manager.
	partitionMgr := parsync.NewPartitionManager(4)

	// Candidate selector.
	selectorConfig := multicandidate.DefaultConfig()
	selectorConfig.DefaultK = 3
	selectorConfig.PenaltyWeight = 0.3

	selector, _ := multicandidate.NewCandidateSelector(
		selectorConfig,
		multicandidate.WithStatsProvider(statsCache),
	)

	// Sync scheduler.
	syncScheduler := parsync.NewSyncScheduler(4, 3, 3*time.Second)

	// 2. Simulate the scheduling process.

	// Assign nodes to partitions.
	nodes := []string{"node-1", "node-2", "node-3", "node-4", "node-5"}
	for _, node := range nodes {
		partitionID := partitionMgr.AssignNode(node)
		fmt.Printf("Node %s assigned to partition %d\n", node, partitionID)
	}

	// Simulate a scheduler adapter.
	adapter := &mockSchedulerAdapter{
		scores: []types.NodeScore{
			{NodeName: "node-1", Score: 100},
			{NodeName: "node-2", Score: 95},
			{NodeName: "node-3", Score: 90},
			{NodeName: "node-4", Score: 85},
			{NodeName: "node-5", Score: 80},
		},
		schedulerID: 0,
		clusterSize: 100,
		podKey:      "default/test-pod",
	}

	// Select candidates.
	candidates, err := selector.SelectCandidates(adapter)
	if err != nil {
		fmt.Printf("Selection failed: %v\n", err)
		return
	}

	fmt.Printf("\nSelected %d candidates:\n", len(candidates.Candidates))
	for _, c := range candidates.Candidates {
		fmt.Printf("  Rank %d: %s (score=%d, conflict=%.2f, fresh=%.2f)\n",
			c.Rank, c.NodeName, c.Score, c.ConflictRate, c.Freshness)
	}

	// 3. Simulate a binding and update stats.

	// Assume the binding succeeded (primary candidate adopted).
	result := types.BindingResult{
		PodKey:      "default/test-pod",
		NodeName:    "node-1",
		Rank:        0,
		Success:     true,
		Timestamp:   time.Now(),
		PartitionID: partitionMgr.GetPartitionID("node-1"),
	}

	statsCache.UpdateResult(result)

	// Display updated stats.
	globalStats := statsCache.GetStats()
	fmt.Printf("\nStats after binding:\n")
	fmt.Printf("  Total: %d, Success: %d, Failure: %d\n",
		globalStats.TotalBindings, globalStats.SuccessCount, globalStats.FailureCount)
	fmt.Printf("  Rank distribution: %v\n", globalStats.RankDistribution)

	// 4. Compute sync schedule.

	schedule := syncScheduler.CalculateSchedule(0, []int{0, 3})
	fmt.Printf("\nSync schedule for scheduler 0:\n")
	for _, slot := range schedule.Slots {
		nextSync := syncScheduler.GetNextSyncTime(schedule, slot.PartitionID)
		fmt.Printf("  Partition %d: offset=%v, next_sync=%v\n",
			slot.PartitionID, slot.Offset, nextSync.Format("15:04:05.000"))
	}

	// 5. Show the currently freshest partition.
	freshPartition := syncScheduler.CalculateFreshPartition(0)
	fmt.Printf("\nFreshest partition for scheduler 0: %d\n", freshPartition)
}

// mockSchedulerAdapter is a mock scheduler adapter for the example.
type mockSchedulerAdapter struct {
	scores      []types.NodeScore
	schedulerID int
	clusterSize int
	podKey      string
}

func (m *mockSchedulerAdapter) GetNodeScores() []types.NodeScore { return m.scores }
func (m *mockSchedulerAdapter) GetClusterSize() int              { return m.clusterSize }
func (m *mockSchedulerAdapter) GetSchedulerID() int              { return m.schedulerID }
func (m *mockSchedulerAdapter) IsHighPriorityPod() bool          { return false }
func (m *mockSchedulerAdapter) GetPodKey() string                { return m.podKey }
