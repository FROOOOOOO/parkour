package parsync

import (
	"math"
	"time"

	"example.com/scheduler-lib/types"
)

// FreshnessCalculator computes partition freshness values.
type FreshnessCalculator struct {
	syncPeriod time.Duration
	tau        float64 // exponential-decay time constant
}

// NewFreshnessCalculator creates a FreshnessCalculator for the given sync period.
func NewFreshnessCalculator(syncPeriod time.Duration) *FreshnessCalculator {
	return &FreshnessCalculator{
		syncPeriod: syncPeriod,
		tau:        syncPeriod.Seconds() / 2, // freshness ≈ 0.37 at half a period
	}
}

// CalculateFreshness returns the freshness [0, 1] for a partition last synced
// at lastSyncTime, using an exponential-decay model.
func (fc *FreshnessCalculator) CalculateFreshness(lastSyncTime time.Time) float64 {
	staleness := time.Since(lastSyncTime)
	return fc.CalculateFreshnessFromStaleness(staleness)
}

// CalculateFreshnessFromStaleness converts a staleness duration to a freshness value.
func (fc *FreshnessCalculator) CalculateFreshnessFromStaleness(staleness time.Duration) float64 {
	if staleness <= 0 {
		return 1.0
	}

	seconds := staleness.Seconds()
	freshness := math.Exp(-seconds / fc.tau)

	return freshness
}

// CalculateStaleness returns how stale a partition is.
func (fc *FreshnessCalculator) CalculateStaleness(lastSyncTime time.Time) time.Duration {
	return time.Since(lastSyncTime)
}

// IsFresh reports whether a partition's freshness exceeds the given threshold.
func (fc *FreshnessCalculator) IsFresh(lastSyncTime time.Time, threshold float64) bool {
	return fc.CalculateFreshness(lastSyncTime) >= threshold
}

// GetExpectedStaleness returns the expected staleness in ParSync mode.
// In ParSync mode the average staleness is approximately G/2.
func (fc *FreshnessCalculator) GetExpectedStaleness() time.Duration {
	return fc.syncPeriod / 2
}

// GetMaxStaleness returns the maximum staleness in ParSync mode.
// In ParSync mode it is approximately 2G/3 (better than the traditional G).
func (fc *FreshnessCalculator) GetMaxStaleness(numSchedulers int) time.Duration {
	if numSchedulers <= 1 {
		return fc.syncPeriod
	}
	// Approximation: G * (1 - 1/(2*N))
	factor := 1.0 - 1.0/(2.0*float64(numSchedulers))
	return time.Duration(float64(fc.syncPeriod) * factor)
}

// ScoreFreshness converts a freshness value to a score bonus.
//
//	freshness 1.0 → maxBonus
//	freshness 0.5 → maxBonus/2
//	freshness 0.0 → 0
func (fc *FreshnessCalculator) ScoreFreshness(freshness float64, maxBonus int64) int64 {
	return int64(freshness * float64(maxBonus))
}

// PartitionFreshnessInfo holds freshness information for a single partition.
type PartitionFreshnessInfo struct {
	PartitionID  int
	LastSyncTime time.Time
	Staleness    time.Duration
	Freshness    float64
	IsFresh      bool
}

// CalculateAllPartitionFreshness computes freshness information for all partitions.
func (fc *FreshnessCalculator) CalculateAllPartitionFreshness(
	partitionInfos map[int]*types.PartitionInfo,
	freshnessThreshold float64,
) []PartitionFreshnessInfo {

	result := make([]PartitionFreshnessInfo, 0, len(partitionInfos))

	for id, info := range partitionInfos {
		staleness := time.Since(info.LastSyncTime)
		freshness := fc.CalculateFreshnessFromStaleness(staleness)

		result = append(result, PartitionFreshnessInfo{
			PartitionID:  id,
			LastSyncTime: info.LastSyncTime,
			Staleness:    staleness,
			Freshness:    freshness,
			IsFresh:      freshness >= freshnessThreshold,
		})
	}

	return result
}
