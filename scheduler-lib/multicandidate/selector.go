package multicandidate

import (
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// CandidateSelector selects candidate nodes for a pod.
type CandidateSelector struct {
	config   *Config
	strategy SelectionStrategy

	// optional external providers
	statsProvider     types.AdoptionStatsProvider
	partitionProvider types.PartitionStateProvider

	mu sync.RWMutex
}

// NewCandidateSelector creates a CandidateSelector.
func NewCandidateSelector(config *Config, opts ...SelectorOption) (*CandidateSelector, error) {
	if config == nil {
		config = DefaultConfig()
	}

	if err := config.Validate(); err != nil {
		return nil, fmt.Errorf("invalid config: %w", err)
	}

	cs := &CandidateSelector{
		config:   config,
		strategy: NewQualityFirstStrategy(config.PenaltyWeight),
	}

	// Apply options.
	for _, opt := range opts {
		opt(cs)
	}

	return cs, nil
}

// SelectorOption is a functional option for CandidateSelector.
type SelectorOption func(*CandidateSelector)

// WithStrategy sets the selection strategy.
func WithStrategy(strategy SelectionStrategy) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.strategy = strategy
	}
}

// WithStatsProvider sets the stats provider.
func WithStatsProvider(provider types.AdoptionStatsProvider) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.statsProvider = provider
	}
}

// WithPartitionProvider sets the partition provider.
func WithPartitionProvider(provider types.PartitionStateProvider) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.partitionProvider = provider
	}
}

// SelectCandidates is the core method that selects candidate nodes for a pod.
func (cs *CandidateSelector) SelectCandidates(adapter types.SchedulerAdapter) (*types.CandidateList, error) {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	// 1. Get node scores.
	nodeScores := adapter.GetNodeScores()
	if len(nodeScores) == 0 {
		return nil, types.ErrNoAvailableNodes
	}

	// 2. Sort by score descending.
	sort.Slice(nodeScores, func(i, j int) bool {
		return nodeScores[i].Score > nodeScores[j].Score
	})

	// 3. Compute dynamic K.
	k := cs.calculateK(adapter, len(nodeScores))

	// 4. Filter by score threshold.
	filtered := cs.filterByScoreThreshold(nodeScores)
	if len(filtered) == 0 {
		return nil, &types.SelectionError{
			Reason:    "no nodes pass score threshold",
			NodeCount: len(nodeScores),
			Threshold: cs.config.ScoreThreshold,
		}
	}

	// 5. Build the candidate list (attach conflict rate and partition info).
	candidates := cs.buildCandidates(filtered, adapter)

	// 6. Apply the strategy to select the final candidates.
	selected := cs.strategy.Select(candidates, k)

	// 7. Build and return the result.
	result := &types.CandidateList{
		Candidates:    selected,
		PodKey:        adapter.GetPodKey(),
		SchedulerID:   adapter.GetSchedulerID(),
		SelectionTime: time.Now(),
	}

	return result, nil
}

// calculateK dynamically computes the number of candidates to select.
func (cs *CandidateSelector) calculateK(adapter types.SchedulerAdapter, availableNodes int) int {
	k := cs.config.DefaultK

	if !cs.config.EnableDynamicK {
		return cs.clampK(k, availableNodes)
	}

	// High-priority pods get extra candidates.
	if adapter.IsHighPriorityPod() {
		k += cs.config.HighPriorityExtraK
	}

	// Large clusters get extra candidates.
	clusterSize := adapter.GetClusterSize()
	if clusterSize > cs.config.LargeClusterThreshold {
		k += cs.config.LargeClusterExtraK
	}

	// Reduce K when available nodes are scarce to avoid selecting poor-quality nodes.
	if availableNodes < k*2 {
		k = (availableNodes + 1) / 2
	}

	return cs.clampK(k, availableNodes)
}

// clampK clamps K to the valid range.
func (cs *CandidateSelector) clampK(k, availableNodes int) int {
	if k < cs.config.MinCandidates {
		k = cs.config.MinCandidates
	}
	if k > cs.config.MaxCandidates {
		k = cs.config.MaxCandidates
	}
	if k > availableNodes {
		k = availableNodes
	}
	return k
}

// filterByScoreThreshold filters nodes by score threshold.
func (cs *CandidateSelector) filterByScoreThreshold(nodeScores []types.NodeScore) []types.NodeScore {
	if len(nodeScores) == 0 {
		return nil
	}

	maxScore := nodeScores[0].Score
	threshold := int64(float64(maxScore) * cs.config.ScoreThreshold)

	filtered := make([]types.NodeScore, 0)
	prevScore := int64(-1)

	for _, ns := range nodeScores {
		// Stop when a score drops below the threshold.
		if ns.Score < threshold {
			break
		}

		// Stop adding when the score gap is too small (avoids selecting too
		// many similar nodes), but always keep at least MinCandidates.
		if prevScore >= 0 && prevScore-ns.Score < cs.config.MinScoreDiff {
			if len(filtered) >= cs.config.MinCandidates {
				break
			}
		}

		filtered = append(filtered, ns)
		prevScore = ns.Score
	}

	return filtered
}

// buildCandidates builds the candidate list and fills PartitionID / Freshness /
// ConflictRate.
func (cs *CandidateSelector) buildCandidates(nodeScores []types.NodeScore, adapter types.SchedulerAdapter) []types.CandidateNode {
	candidates := make([]types.CandidateNode, len(nodeScores))

	for i, ns := range nodeScores {
		candidate := types.CandidateNode{
			NodeScore: ns,
			Rank:      i,
		}

		// Partition info (used by LatencyFirst for Freshness).
		if cs.partitionProvider != nil {
			partitionID := cs.partitionProvider.GetPartitionID(ns.NodeName)
			candidate.PartitionID = int(partitionID)

			staleness := cs.partitionProvider.GetPartitionStaleness(partitionID)
			candidate.Freshness = cs.calculateFreshness(staleness)
		} else {
			candidate.PartitionID = -1
			candidate.Freshness = 1.0 // assume fresh when no partition info is available
		}

		// Node conflict rate (used by QualityFirst / WeightedRandom scoring
		// formulas). When provider is nil or there are no cold-start samples,
		// ConflictRate=0 degrades the strategy to pure Score ordering.
		if cs.statsProvider != nil {
			candidate.ConflictRate = cs.statsProvider.GetNodeConflictRate(ns.NodeName)
		}

		candidate.Reason = fmt.Sprintf("score=%d, cr=%.2f, fresh=%.2f, part=%d",
			candidate.Score, candidate.ConflictRate, candidate.Freshness, candidate.PartitionID)

		candidates[i] = candidate
	}

	return candidates
}

// calculateFreshness converts a staleness duration to a freshness value [0, 1]
// using exponential decay.
//
//	freshness = exp(-staleness / tau)
//	tau = 10s: staleness=0 → 1.0, staleness=10s → 0.37, staleness=20s → 0.14
func (cs *CandidateSelector) calculateFreshness(staleness time.Duration) float64 {
	const tau = 10.0 // seconds

	seconds := staleness.Seconds()
	if seconds <= 0 {
		return 1.0
	}

	freshness := math.Exp(-seconds / tau)
	return freshness
}

// UpdateConfig replaces the current configuration.
func (cs *CandidateSelector) UpdateConfig(config *Config) error {
	if err := config.Validate(); err != nil {
		return err
	}

	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.config = config
	return nil
}

// SetStrategy replaces the selection strategy.
func (cs *CandidateSelector) SetStrategy(strategy SelectionStrategy) {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.strategy = strategy
}

// GetConfig returns a copy of the current configuration.
func (cs *CandidateSelector) GetConfig() *Config {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	configCopy := *cs.config
	return &configCopy
}
