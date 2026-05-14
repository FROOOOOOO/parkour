package stats

// ProbabilityCalculator provides probability utility functions.
//
// Phase 2 simplification: the functions CalculateAdoptionProbability,
// CalculateUpdateProbability, ExponentialDecay, NormalizeProbabilities, and
// Entropy that were originally kept for the local-state update strategy
// (deprecated) and the early Penalty formula are no longer referenced after
// the strategy migrated to the paper formula
// `(1-p)*normScore + p*(1-conflictRate)`, and have been removed.
// Only CalculateConflictRate is retained, reused by AdoptionStatsCache.updateSummary
// and the AdoptionStats CRD watcher.
type ProbabilityCalculator struct{}

func NewProbabilityCalculator() *ProbabilityCalculator {
	return &ProbabilityCalculator{}
}

// CalculateConflictRate computes the cumulative conflict rate as
// failure / (success + failure).
// Returns 0 when successCount+failureCount==0 (cold-start produces no spurious data).
func (pc *ProbabilityCalculator) CalculateConflictRate(successCount, failureCount int64) float64 {
	total := successCount + failureCount
	if total == 0 {
		return 0
	}
	return float64(failureCount) / float64(total)
}
