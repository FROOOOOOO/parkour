// scheduler-lib/multicandidate/strategy.go
package multicandidate

import (
	"math"
	"math/rand"
	"sort"

	"example.com/scheduler-lib/types"
)

// Strategy name constants (used for factory lookup).
const (
	QualityFirst        = "QualityFirst"
	LatencyFirst        = "LatencyFirst"
	WeightedRandom      = "WeightedRandom"
	QualityFirstParSync = "QualityFirstParSync"
	LatencyFirstParSync = "LatencyFirstParSync"
)

// SelectionStrategy is the interface for candidate selection strategies.
type SelectionStrategy interface {
	// Name returns the strategy name.
	Name() string

	// Select selects up to k candidates from the given list.
	Select(candidates []types.CandidateNode, k int) []types.CandidateNode

	// Reorder re-ranks candidates in place.
	Reorder(candidates []types.CandidateNode)
}

// === QualityFirst ===

// QualityFirstStrategy is the quality-first strategy with conflict-rate penalty.
//
// Scoring formula (consistent with the paper's Penalty definition):
//
//	adjusted = (1-p) * normalized_score + p * (1 - conflictRate)
//
// where normalized_score = score / maxScore ∈ [0,1] and adjusted ∈ [0,1].
//   - p=0: degrades to pure original-Score ordering (Penalty disabled)
//   - p=1: orders purely by (1 - conflictRate), ignoring the original score
//
// Note: not equivalent to the multiplicative form score*(1-p*cr) used by
// applyConflictPenalty in the old k8s-scheduler; the Phase 1 migration fixed
// this historical inconsistency with the paper formula.
type QualityFirstStrategy struct {
	penaltyWeight float64
}

func NewQualityFirstStrategy(penaltyWeight float64) *QualityFirstStrategy {
	return &QualityFirstStrategy{penaltyWeight: penaltyWeight}
}

func (s *QualityFirstStrategy) Name() string {
	return QualityFirst
}

func (s *QualityFirstStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	s.Reorder(candidates)
	if len(candidates) < k {
		return candidates
	}
	return candidates[:k]
}

func (s *QualityFirstStrategy) Reorder(candidates []types.CandidateNode) {
	lenCandidates := len(candidates)
	if lenCandidates == 0 {
		return
	}

	maxScore := float64(candidates[0].Score)
	if maxScore <= 0 {
		// Defensive: avoid division by zero / negative scores.  All nodes'
		// normalised scores are treated as equal (=1); final ordering is
		// determined solely by (1-conflictRate).
		maxScore = 1
	}
	p := s.penaltyWeight

	type scoredCandidate struct {
		candidate     types.CandidateNode
		combinedScore float64
	}

	scored := make([]scoredCandidate, lenCandidates)
	for i, c := range candidates {
		normalizedScore := float64(c.Score) / maxScore
		if normalizedScore < 0 {
			normalizedScore = 0
		}
		combinedScore := (1-p)*normalizedScore + p*(1-c.ConflictRate)
		scored[i] = scoredCandidate{candidate: c, combinedScore: combinedScore}
	}

	sort.SliceStable(scored, func(i, j int) bool {
		return scored[i].combinedScore > scored[j].combinedScore
	})

	for i := 0; i < lenCandidates; i++ {
		candidates[i] = scored[i].candidate
		candidates[i].Rank = i
	}
}

// === LatencyFirst ===

// LatencyFirstStrategy is the latency-first strategy (aligned with the ParSync
// paper).
//
// Ordering: Freshness descending first; Score descending as a tie-breaker when
// Freshness values are equal.  This strategy does not use PenaltyWeight because
// its goal is to prefer the most up-to-date partition nodes, reducing conflicts
// by "reading fresh data" rather than "penalising high-conflict nodes".
type LatencyFirstStrategy struct{}

func NewLatencyFirstStrategy() *LatencyFirstStrategy {
	return &LatencyFirstStrategy{}
}

func (s *LatencyFirstStrategy) Name() string {
	return LatencyFirst
}

func (s *LatencyFirstStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	s.Reorder(candidates)
	if len(candidates) < k {
		return candidates
	}
	return candidates[:k]
}

func (s *LatencyFirstStrategy) Reorder(candidates []types.CandidateNode) {
	sort.SliceStable(candidates, func(i, j int) bool {
		if math.Abs(candidates[i].Freshness-candidates[j].Freshness) > types.Epsilon {
			return candidates[i].Freshness > candidates[j].Freshness
		}
		return candidates[i].Score > candidates[j].Score
	})
	for i := range candidates {
		candidates[i].Rank = i
	}
}

// === WeightedRandom ===

// WeightedRandomStrategy is the weighted-random strategy.
//
// Weight formula (identical to QualityFirst):
//
//	weight = (1-p) * normalized_score + p * (1 - conflictRate)  ∈ [0,1]
//
// The only difference from QualityFirst is the selection method: QualityFirst
// picks deterministically in descending weight order, while WeightedRandom
// uses weight as sampling probabilities (without replacement), introducing
// diversity so that multiple scheduler instances do not always resolve ties the
// same way, spreading load more evenly.
//
// Edge cases: p=0 with equal maxScores degrades to pure uniform sampling;
// totalWeight==0 also degrades to uniform sampling.
type WeightedRandomStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewWeightedRandomStrategy(seed int64, penaltyWeight float64) *WeightedRandomStrategy {
	return &WeightedRandomStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *WeightedRandomStrategy) Name() string {
	return WeightedRandom
}

func (s *WeightedRandomStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	if len(candidates) <= k {
		// Fewer candidates than requested: return all in original Score order
		// (caller has already passed them in descending order).
		out := make([]types.CandidateNode, len(candidates))
		copy(out, candidates)
		for i := range out {
			out[i].Rank = i
		}
		return out
	}

	pool := make([]types.CandidateNode, len(candidates))
	copy(pool, candidates)

	maxScore := float64(pool[0].Score)
	if maxScore <= 0 {
		maxScore = 1
	}
	p := s.penaltyWeight

	result := make([]types.CandidateNode, 0, k)
	for len(result) < k && len(pool) > 0 {
		weights := make([]float64, len(pool))
		totalWeight := 0.0
		for i, c := range pool {
			normScore := float64(c.Score) / maxScore
			if normScore < 0 {
				normScore = 0
			}
			w := (1-p)*normScore + p*(1-c.ConflictRate)
			if w < 0 {
				w = 0
			}
			weights[i] = w
			totalWeight += w
		}

		// Weighted sampling (degrades to uniform when totalWeight==0).
		var selectedIdx int
		if totalWeight == 0 {
			selectedIdx = s.rng.Intn(len(pool))
		} else {
			r := s.rng.Float64() * totalWeight
			cumulative := 0.0
			for i, w := range weights {
				cumulative += w
				if cumulative >= r {
					selectedIdx = i
					break
				}
			}
		}

		selected := pool[selectedIdx]
		selected.Rank = len(result)
		result = append(result, selected)
		pool = append(pool[:selectedIdx], pool[selectedIdx+1:]...)
	}

	return result
}

// Reorder is a no-op for WeightedRandom (random sampling is done in Select).
func (s *WeightedRandomStrategy) Reorder(candidates []types.CandidateNode) {}

// === QualityFirstParSync ===

// QualityFirstParSyncStrategy is the Quality-first strategy from the ParSync
// paper (ATC'21), partition-aware edition.  Algorithm:
//
//  1. Group candidates by PartitionID.
//  2. Compute the mean adjustedScore within each partition; sort partitions by
//     mean in descending order.
//  3. Starting from the best partition, sample nodes within the group by
//     adjustedScore without replacement until K candidates are filled; if the
//     current partition has too few nodes, continue to the next best partition.
//
// adjustedScore = (1-p)*normalizedScore + p*(1-conflictRate), sharing the
// conflict-rate penalty path with QualityFirst; p=0 gives the pure paper version.
//
// Key difference from QualityFirstStrategy (project experiment baseline):
//   - QualityFirst is candidate-grain top-K (aligned with the K8s default
//     scheduler; always returns the K highest-scoring nodes).
//   - QualityFirstParSync is partition-grain; it may not always return the K
//     globally highest-scoring nodes due to partition constraints, but it more
//     faithfully implements the strategy definition in paper §6.1.
type QualityFirstParSyncStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewQualityFirstParSyncStrategy(seed int64, penaltyWeight float64) *QualityFirstParSyncStrategy {
	return &QualityFirstParSyncStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *QualityFirstParSyncStrategy) Name() string { return QualityFirstParSync }

func (s *QualityFirstParSyncStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	maxScore := maxOriginalScore(candidates)
	groups := groupByPartition(candidates)

	// Sort partitions by mean adjustedScore descending; on a tie, ascending
	// PartitionID for reproducibility (groupByPartition returns groups in
	// ascending PartitionID order; stable sort preserves that as tie-break).
	sort.SliceStable(groups, func(i, j int) bool {
		return partitionAvgAdjusted(groups[i], maxScore, s.penaltyWeight) >
			partitionAvgAdjusted(groups[j], maxScore, s.penaltyWeight)
	})
	return fillFromPartitions(groups, k, maxScore, s.penaltyWeight, s.rng)
}

// Reorder is a no-op for ParSync strategies: the random-sampling semantic is
// handled by Select; reordering a flat candidates slice has no "partition-grain"
// meaning.
func (s *QualityFirstParSyncStrategy) Reorder(candidates []types.CandidateNode) {}

// === LatencyFirstParSync ===

// LatencyFirstParSyncStrategy is the Latency-first strategy from the ParSync
// paper (ATC'21), partition-aware edition.  Algorithm:
//
//  1. Group candidates by PartitionID.
//  2. Sort partitions by freshness descending (all nodes within a partition
//     share the same freshness; use the first node as representative).
//  3. Starting from the freshest partition, sample nodes within the group by
//     adjustedScore without replacement until K candidates are filled; fall
//     back to the next freshest partition if needed.
//
// adjustedScore is the same as in QualityFirstParSync, so PenaltyWeight>0 also
// influences the intra-partition ordering in LatencyFirst.  The paper does not
// specify how to pick within a partition for LF; "score-weighted sampling" is
// used here for symmetry with QF.
//
// Key difference from LatencyFirstStrategy (project experiment baseline):
//   - LatencyFirst is candidate-grain: all candidates are sorted once by
//     (freshness, score) and top-K is taken; nodes from different partitions
//     can be mixed when freshness values are close.
//   - LatencyFirstParSync is partition-grain: strictly fills the freshest
//     partition before falling back, never mixing partitions.
type LatencyFirstParSyncStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewLatencyFirstParSyncStrategy(seed int64, penaltyWeight float64) *LatencyFirstParSyncStrategy {
	return &LatencyFirstParSyncStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *LatencyFirstParSyncStrategy) Name() string { return LatencyFirstParSync }

func (s *LatencyFirstParSyncStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	maxScore := maxOriginalScore(candidates)
	groups := groupByPartition(candidates)

	// Sort partitions by Freshness descending (all nodes in the same partition
	// have identical Freshness; use the first node as representative).
	// On equal freshness, ascending PartitionID for reproducibility (stable sort).
	sort.SliceStable(groups, func(i, j int) bool {
		return groups[i][0].Freshness > groups[j][0].Freshness
	})
	return fillFromPartitions(groups, k, maxScore, s.penaltyWeight, s.rng)
}

func (s *LatencyFirstParSyncStrategy) Reorder(candidates []types.CandidateNode) {}

// === ParSync helpers ===

// maxOriginalScore returns the largest original Score among candidates, used as
// the normalisation denominator.  Returns 1 when all scores are ≤0, so that
// normScore=1 does not participate in sorting and the final order is determined
// by conflictRate alone (consistent with QualityFirstStrategy's defensive logic).
func maxOriginalScore(candidates []types.CandidateNode) float64 {
	max := int64(0)
	for _, c := range candidates {
		if c.Score > max {
			max = c.Score
		}
	}
	if max <= 0 {
		return 1
	}
	return float64(max)
}

// groupByPartition groups candidates by PartitionID, preserving intra-group
// order.  The returned slice of groups is sorted by PartitionID ascending,
// giving subsequent stable sorts a reproducible tie-break baseline.
func groupByPartition(candidates []types.CandidateNode) [][]types.CandidateNode {
	idx := make(map[int]int)
	groups := make([][]types.CandidateNode, 0)
	for _, c := range candidates {
		i, ok := idx[c.PartitionID]
		if !ok {
			i = len(groups)
			idx[c.PartitionID] = i
			groups = append(groups, []types.CandidateNode{})
		}
		groups[i] = append(groups[i], c)
	}
	sort.SliceStable(groups, func(i, j int) bool {
		return groups[i][0].PartitionID < groups[j][0].PartitionID
	})
	return groups
}

// adjustedScore computes the combined score for a single node:
// (1-p)*normScore + p*(1-conflictRate) ∈ [0,1].
func adjustedScore(c types.CandidateNode, maxScore, p float64) float64 {
	norm := float64(c.Score) / maxScore
	if norm < 0 {
		norm = 0
	}
	return (1-p)*norm + p*(1-c.ConflictRate)
}

// partitionAvgAdjusted returns the mean adjustedScore for a partition group.
// Returns 0 for empty groups (cannot happen — groupByPartition never produces
// empty groups — but kept as a defensive measure).
func partitionAvgAdjusted(group []types.CandidateNode, maxScore, p float64) float64 {
	if len(group) == 0 {
		return 0
	}
	sum := 0.0
	for _, c := range group {
		sum += adjustedScore(c, maxScore, p)
	}
	return sum / float64(len(group))
}

// fillFromPartitions iterates over groups in order and performs weighted
// without-replacement sampling within each partition until k candidates are
// accumulated.  Falls through to the next partition when the current one has
// fewer nodes than needed.  The Rank field is assigned in final selection order
// (0=primary).
func fillFromPartitions(groups [][]types.CandidateNode, k int, maxScore, p float64, rng *rand.Rand) []types.CandidateNode {
	result := make([]types.CandidateNode, 0, k)
	for _, group := range groups {
		if len(result) >= k {
			break
		}
		need := k - len(result)
		picked := weightedSampleNoReplace(group, need, maxScore, p, rng)
		result = append(result, picked...)
	}
	for i := range result {
		result[i].Rank = i
	}
	return result
}

// weightedSampleNoReplace performs n weighted without-replacement samples from
// pool using adjustedScore as the weight.  Degrades to uniform sampling when
// totalWeight==0 (rare: all nodes have adjustedScore 0).  Returns the entire
// pool when n >= len(pool).
func weightedSampleNoReplace(pool []types.CandidateNode, n int, maxScore, p float64, rng *rand.Rand) []types.CandidateNode {
	if n <= 0 || len(pool) == 0 {
		return nil
	}
	if n >= len(pool) {
		out := make([]types.CandidateNode, len(pool))
		copy(out, pool)
		return out
	}
	work := make([]types.CandidateNode, len(pool))
	copy(work, pool)

	out := make([]types.CandidateNode, 0, n)
	for len(out) < n && len(work) > 0 {
		weights := make([]float64, len(work))
		total := 0.0
		for i, c := range work {
			w := adjustedScore(c, maxScore, p)
			if w < 0 {
				w = 0
			}
			weights[i] = w
			total += w
		}
		var idx int
		if total == 0 {
			idx = rng.Intn(len(work))
		} else {
			r := rng.Float64() * total
			cum := 0.0
			for i, w := range weights {
				cum += w
				if cum >= r {
					idx = i
					break
				}
			}
		}
		out = append(out, work[idx])
		work = append(work[:idx], work[idx+1:]...)
	}
	return out
}
