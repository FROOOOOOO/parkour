package stats

import (
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// AdoptionStatsCache caches adoption statistics.
type AdoptionStatsCache struct {
	mu sync.RWMutex

	// configuration
	config *StatsConfig

	// global counts: P[i] = number of times the i-th candidate was adopted
	// P[n+1] = number of all-candidates-failed events
	globalCounts []int64

	// per-partition counts
	partitionCounts map[int][]int64

	// per-node counts (for node-level probability adjustment)
	nodeStats map[string]*NodeAdoptionStats

	// sliding windows (for computing recent success rates)
	globalWindow     *SlidingWindow
	partitionWindows map[int]*SlidingWindow

	// probability calculator
	calculator *ProbabilityCalculator

	// statistics summary
	summary *types.AdoptionStats

	// last update time
	lastUpdateTime time.Time
}

// StatsConfig holds configuration for AdoptionStatsCache.
type StatsConfig struct {
	// MaxCandidates is the maximum number of candidates (determines the counts array size).
	MaxCandidates int

	// WindowSize is the size of the sliding window.
	WindowSize int

	// EnableNodeStats controls whether per-node statistics are collected.
	EnableNodeStats bool

	// EnablePartitionStats controls whether per-partition statistics are collected.
	EnablePartitionStats bool

	// DecayInterval is the interval between decay runs.
	DecayInterval time.Duration

	// DecayFactor is the multiplicative decay factor applied each interval.
	DecayFactor float64
}

// DefaultStatsConfig returns a StatsConfig with sensible defaults.
func DefaultStatsConfig() *StatsConfig {
	return &StatsConfig{
		MaxCandidates:        10,
		WindowSize:           1000,
		EnableNodeStats:      true,
		EnablePartitionStats: true,
		DecayInterval:        time.Minute,
		DecayFactor:          0.95,
	}
}

// NodeAdoptionStats tracks per-node adoption statistics.
type NodeAdoptionStats struct {
	NodeName     string
	SuccessCount int64
	FailureCount int64
	LastSuccess  time.Time
	LastFailure  time.Time
	Window       *SlidingWindow
}

// NewAdoptionStatsCache creates a new AdoptionStatsCache.
func NewAdoptionStatsCache(config *StatsConfig) *AdoptionStatsCache {
	if config == nil {
		config = DefaultStatsConfig()
	}

	// counts array size = MaxCandidates + 1 (last slot = all-candidates-failed)
	countsSize := config.MaxCandidates + 1

	cache := &AdoptionStatsCache{
		config:           config,
		globalCounts:     make([]int64, countsSize),
		partitionCounts:  make(map[int][]int64),
		nodeStats:        make(map[string]*NodeAdoptionStats),
		globalWindow:     NewSlidingWindow(config.WindowSize),
		partitionWindows: make(map[int]*SlidingWindow),
		calculator:       NewProbabilityCalculator(),
		summary:          &types.AdoptionStats{RankDistribution: make([]int64, countsSize)},
		lastUpdateTime:   time.Now(),
	}

	return cache
}

// UpdateResult records a binding result.
//
// Three calling semantics (determined by the combination of
// BindingResult.Success and NodeName):
//
//	(a) Success=true,  NodeName=X    → pod bound successfully to node X.
//	                                   Updates global/partition/node.
//	(b) Success=false, NodeName=""   → all candidates failed (ACF).
//	                                   Updates global/partition (counts as a
//	                                   pod-level FailureCount and
//	                                   globalCounts[last]); does NOT touch
//	                                   nodeStats (no specific node to blame).
//	(c) Success=false, NodeName=X    → candidate-level failure: a single
//	                                   candidate node's bind call failed, but
//	                                   the overall pod may still succeed via
//	                                   another candidate.  Only updates
//	                                   nodeStats[X]; does NOT touch
//	                                   global/partition summaries — preserving
//	                                   the "pod-level" semantics of
//	                                   SuccessCount/FailureCount and avoiding
//	                                   the double-counting bug where one ACF pod
//	                                   is recorded as K+1 failures + 1 ACF.
//
// As a result:
//   - global/partition dimension: pod-level statistics (matches the Prometheus
//     "ACF" semantic)
//   - node dimension: attempt-level statistics (matches the Prometheus "binder
//     conflict" semantic), used by the penalty mechanism for per-node scoring.
func (c *AdoptionStatsCache) UpdateResult(result types.BindingResult) {
	c.mu.Lock()
	defer c.mu.Unlock()

	isCandidateLevelFailure := !result.Success && result.NodeName != ""

	if !isCandidateLevelFailure {
		// 1. Update global statistics (pod-level events only).
		if result.Success {
			rank := result.Rank
			if rank >= 0 && rank < len(c.globalCounts)-1 {
				c.globalCounts[rank]++
			}
			c.globalWindow.Add(true)
			c.summary.SuccessCount++
		} else {
			// All candidates failed — increment the last slot.
			c.globalCounts[len(c.globalCounts)-1]++
			c.globalWindow.Add(false)
			c.summary.FailureCount++
		}
		c.summary.TotalBindings++

		// 2. Update per-partition statistics (pod-level events only).
		if c.config.EnablePartitionStats {
			c.updatePartitionStats(result)
		}
	}

	// 3. Update per-node statistics (all events that carry a NodeName: success
	//    or candidate-level failure).
	if c.config.EnableNodeStats && result.NodeName != "" {
		c.updateNodeStats(result)
	}

	// 4. Refresh the summary.
	c.updateSummary()

	c.lastUpdateTime = time.Now()
}

// updatePartitionStats updates per-partition counters.
func (c *AdoptionStatsCache) updatePartitionStats(result types.BindingResult) {
	partitionID := result.PartitionID

	// Ensure the partition counts array exists.
	if _, exists := c.partitionCounts[partitionID]; !exists {
		c.partitionCounts[partitionID] = make([]int64, len(c.globalCounts))
		c.partitionWindows[partitionID] = NewSlidingWindow(c.config.WindowSize / 4) // hyperparameter
	}

	counts := c.partitionCounts[partitionID]
	window := c.partitionWindows[partitionID]

	if result.Success {
		rank := result.Rank
		if rank >= 0 && rank < len(counts)-1 {
			counts[rank]++
		}
		window.Add(true)
	} else {
		counts[len(counts)-1]++
		window.Add(false)
	}
}

// updateNodeStats updates per-node counters.
func (c *AdoptionStatsCache) updateNodeStats(result types.BindingResult) {
	nodeName := result.NodeName

	stats, exists := c.nodeStats[nodeName]
	if !exists {
		stats = &NodeAdoptionStats{
			NodeName: nodeName,
			Window:   NewSlidingWindow(100), // hyperparameter
		}
		c.nodeStats[nodeName] = stats
	}

	if result.Success {
		stats.SuccessCount++
		stats.LastSuccess = result.Timestamp
		stats.Window.Add(true)
	} else {
		stats.FailureCount++
		stats.LastFailure = result.Timestamp
		stats.Window.Add(false)
	}
}

// updateSummary refreshes the statistics summary.
//
// ConflictRate is computed from the SlidingWindow's recent samples, reflecting
// the "recent conflict rate" rather than the cumulative rate since startup.
// Returns 0 when the window contains no samples (consistent with the cold-start
// behaviour of CalculateConflictRate, ensuring that the Penalty does not
// activate immediately due to spurious data at startup).
func (c *AdoptionStatsCache) updateSummary() {
	// Copy rank distribution.
	copy(c.summary.RankDistribution, c.globalCounts)

	// Compute average attempts.
	totalAttempts := int64(0)
	for rank, count := range c.globalCounts[:len(c.globalCounts)-1] {
		totalAttempts += int64(rank+1) * count
	}
	// Failed pods count as MaxCandidates attempts.
	totalAttempts += int64(c.config.MaxCandidates) * c.globalCounts[len(c.globalCounts)-1]

	if c.summary.TotalBindings > 0 {
		c.summary.AvgAttempts = float64(totalAttempts) / float64(c.summary.TotalBindings)
	}

	// Compute conflict rate: prefer the sliding window's recent samples;
	// fall back to the cumulative conflict rate when the window is empty
	// (also covers the cold-start case, returning 0).
	if c.globalWindow != nil && c.globalWindow.Count() > 0 {
		c.summary.ConflictRate = 1.0 - c.globalWindow.SuccessRate()
	} else {
		c.summary.ConflictRate = c.calculator.CalculateConflictRate(
			c.summary.SuccessCount, c.summary.FailureCount)
	}
}

// GetNodeConflictRate returns the recent conflict rate [0,1] for the given node.
//
// Data source: (1 - successRate) of the node-level SlidingWindow.
// Returns 0 during cold-start or when the window contains no samples,
// consistent with updateSummary's global ConflictRate cold-start behaviour to
// prevent the Penalty from activating on spurious data at startup.
//
// Implements types.AdoptionStatsProvider.
func (c *AdoptionStatsCache) GetNodeConflictRate(nodeName string) float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	s, ok := c.nodeStats[nodeName]
	if !ok || s.Window == nil || s.Window.Count() == 0 {
		return 0
	}
	return 1.0 - s.Window.SuccessRate()
}

// GetStats returns a copy of the current statistics summary.
func (c *AdoptionStatsCache) GetStats() *types.AdoptionStats {
	c.mu.RLock()
	defer c.mu.RUnlock()

	// Return a copy.
	statsCopy := *c.summary
	statsCopy.RankDistribution = make([]int64, len(c.summary.RankDistribution))
	copy(statsCopy.RankDistribution, c.summary.RankDistribution)

	return &statsCopy
}

// GetNodeCountsMap returns [attempts, conflicts] statistics for all nodes,
// used by the Reporter to serialize into the AdoptionStats CRD's NodeCounts
// field so the Scheduler side can compute per-node conflict rates.
//
// By default only nodes that have at least one failure (FailureCount > 0) are
// included.  Rationale:
//   - In HC-1 scenarios with 10 000 nodes, the vast majority have never had a
//     conflict; including all of them would inflate the status payload to MBs.
//   - When a node is absent, the Scheduler can safely default its conflict rate
//     to 0 (conflict_rate=0 in the Penalty formula means "no penalty").
//
// Pass includeZero=true to include all nodes (including those with zero conflicts).
//
// Return value:
//
//	map[nodeName][]int64{attempts, conflicts}
//	where attempts = SuccessCount + FailureCount, conflicts = FailureCount.
//	Both counters are subject to Decay() over time (DecayFactor=0.95/DecayInterval),
//	so they reflect "recent" conflict trends as required by the paper's timeliness
//	requirement for the penalty signal.
func (c *AdoptionStatsCache) GetNodeCountsMap(includeZero bool) map[string][]int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	out := make(map[string][]int64, len(c.nodeStats))
	for name, s := range c.nodeStats {
		if !includeZero && s.FailureCount == 0 {
			continue
		}
		out[name] = []int64{s.SuccessCount + s.FailureCount, s.FailureCount}
	}
	return out
}

// GetGlobalCounts returns a copy of the global counts (for debugging).
func (c *AdoptionStatsCache) GetGlobalCounts() []int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	counts := make([]int64, len(c.globalCounts))
	copy(counts, c.globalCounts)
	return counts
}

// GetPartitionCounts returns a copy of the counts for the given partition.
func (c *AdoptionStatsCache) GetPartitionCounts(partitionID int) []int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if counts, exists := c.partitionCounts[partitionID]; exists {
		result := make([]int64, len(counts))
		copy(result, counts)
		return result
	}
	return nil
}

// LoadSummary restores cumulative counts from a persisted summary (typically
// from a CRD) to prevent a Binder restart from overwriting historical CRD data
// with zeros.
//
// Fields restored:
//   - summary.TotalBindings / SuccessCount / FailureCount
//   - globalCounts (copied up to the shorter of the two lengths, to guard
//     against config changes causing an out-of-bounds access)
//   - partitionCounts
//
// Fields NOT restored (not persisted in the CRD):
//   - SlidingWindow — after restart, the window starts accumulating from empty;
//     updateSummary falls back to CalculateConflictRate(summary.SuccessCount,
//     summary.FailureCount) when the window is empty, which equals the
//     cumulative conflict rate from the CRD — semantically consistent.
//   - nodeStats — not synchronised with the CRD, and never persisted.
//
// Idempotent: overwrites existing values; multiple calls are equivalent to one.
func (c *AdoptionStatsCache) LoadSummary(
	totalBindings, successCount, failureCount int64,
	globalCounts []int64,
	partitionCounts map[int][]int64,
) {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.summary.TotalBindings = totalBindings
	c.summary.SuccessCount = successCount
	c.summary.FailureCount = failureCount

	if len(globalCounts) > 0 {
		n := len(c.globalCounts)
		if len(globalCounts) < n {
			n = len(globalCounts)
		}
		for i := 0; i < n; i++ {
			c.globalCounts[i] = globalCounts[i]
		}
		copy(c.summary.RankDistribution, c.globalCounts)
	}

	if len(partitionCounts) > 0 {
		for pid, counts := range partitionCounts {
			cp := make([]int64, len(counts))
			copy(cp, counts)
			c.partitionCounts[pid] = cp
		}
	}

	// Recompute derived summary fields (ConflictRate, AvgAttempts).
	// When the SlidingWindow is empty, updateSummary uses the cumulative
	// conflict rate branch with the SuccessCount/FailureCount we just restored.
	c.updateSummary()
}

// Reset clears all statistics.
func (c *AdoptionStatsCache) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()

	for i := range c.globalCounts {
		c.globalCounts[i] = 0
	}
	c.partitionCounts = make(map[int][]int64)
	c.nodeStats = make(map[string]*NodeAdoptionStats)
	c.globalWindow.Reset()
	c.partitionWindows = make(map[int]*SlidingWindow)
	c.summary = &types.AdoptionStats{RankDistribution: make([]int64, len(c.globalCounts))}
	c.lastUpdateTime = time.Now()
}

// Decay applies exponential decay to all cumulative counters (typically called
// periodically).
//
// globalCounts, partitionCounts, per-node SuccessCount/FailureCount, and the
// summary's SuccessCount/FailureCount/TotalBindings are all multiplied by
// DecayFactor, giving more weight to recent events.  SlidingWindows already
// implement recency through their ring-buffer design and do not need decay.
func (c *AdoptionStatsCache) Decay() {
	c.mu.Lock()
	defer c.mu.Unlock()

	factor := c.config.DecayFactor

	// Decay global counts.
	for i := range c.globalCounts {
		c.globalCounts[i] = int64(float64(c.globalCounts[i]) * factor)
	}

	// Decay per-partition counts.
	for _, counts := range c.partitionCounts {
		for i := range counts {
			counts[i] = int64(float64(counts[i]) * factor)
		}
	}

	// Decay per-node statistics.
	for _, stats := range c.nodeStats {
		stats.SuccessCount = int64(float64(stats.SuccessCount) * factor)
		stats.FailureCount = int64(float64(stats.FailureCount) * factor)
	}

	// Decay the summary's cumulative fields to keep them consistent with
	// globalCounts (otherwise CRD SuccessCount/FailureCount grow monotonically
	// and CalculateConflictRate on the Scheduler side cannot reflect recent
	// trends).
	c.summary.SuccessCount = int64(float64(c.summary.SuccessCount) * factor)
	c.summary.FailureCount = int64(float64(c.summary.FailureCount) * factor)
	c.summary.TotalBindings = int64(float64(c.summary.TotalBindings) * factor)

	c.updateSummary()
}

// RunDecayLoop periodically invokes Decay() at the interval configured in
// StatsConfig.DecayInterval. Blocks until stop is closed. Callers typically
// invoke this in a goroutine at process start.
//
// When DecayInterval <= 0, the loop exits immediately (decay disabled).
func (c *AdoptionStatsCache) RunDecayLoop(stop <-chan struct{}) {
	if c.config.DecayInterval <= 0 {
		return
	}
	ticker := time.NewTicker(c.config.DecayInterval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			c.Decay()
		}
	}
}
