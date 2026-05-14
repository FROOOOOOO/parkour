package parsync

import (
	"math"
	"time"

	"example.com/scheduler-lib/types"
)

// FreshnessCalculator 新鲜度计算器
type FreshnessCalculator struct {
	syncPeriod time.Duration
	tau        float64 // 衰减时间常数
}

// NewFreshnessCalculator 创建新鲜度计算器
func NewFreshnessCalculator(syncPeriod time.Duration) *FreshnessCalculator {
	return &FreshnessCalculator{
		syncPeriod: syncPeriod,
		tau:        syncPeriod.Seconds() / 2, // 半周期时新鲜度约 0.37
	}
}

// CalculateFreshness 计算分区新鲜度 [0, 1]
// 使用指数衰减模型
func (fc *FreshnessCalculator) CalculateFreshness(lastSyncTime time.Time) float64 {
	staleness := time.Since(lastSyncTime)
	return fc.CalculateFreshnessFromStaleness(staleness)
}

// CalculateFreshnessFromStaleness 从陈旧度计算新鲜度
func (fc *FreshnessCalculator) CalculateFreshnessFromStaleness(staleness time.Duration) float64 {
	if staleness <= 0 {
		return 1.0
	}

	seconds := staleness.Seconds()
	freshness := math.Exp(-seconds / fc.tau)

	return freshness
}

// CalculateStaleness 计算分区陈旧度
func (fc *FreshnessCalculator) CalculateStaleness(lastSyncTime time.Time) time.Duration {
	return time.Since(lastSyncTime)
}

// IsFresh 判断分区是否新鲜（新鲜度 > 阈值）
func (fc *FreshnessCalculator) IsFresh(lastSyncTime time.Time, threshold float64) bool {
	return fc.CalculateFreshness(lastSyncTime) >= threshold
}

// GetExpectedStaleness 计算预期陈旧度
// 在 ParSync 模式下，平均陈旧度约为 G/2
func (fc *FreshnessCalculator) GetExpectedStaleness() time.Duration {
	return fc.syncPeriod / 2
}

// GetMaxStaleness 计算最大陈旧度
// 在 ParSync 模式下约为 2G/3（比传统方案的 G 更优）
func (fc *FreshnessCalculator) GetMaxStaleness(numSchedulers int) time.Duration {
	if numSchedulers <= 1 {
		return fc.syncPeriod
	}
	// 近似值：G * (1 - 1/(2*N))
	factor := 1.0 - 1.0/(2.0*float64(numSchedulers))
	return time.Duration(float64(fc.syncPeriod) * factor)
}

// ScoreFreshness 将新鲜度转换为得分加成
func (fc *FreshnessCalculator) ScoreFreshness(freshness float64, maxBonus int64) int64 {
	// 新鲜度 1.0 -> maxBonus
	// 新鲜度 0.5 -> maxBonus/2
	// 新鲜度 0.0 -> 0
	return int64(freshness * float64(maxBonus))
}

// PartitionFreshnessInfo 分区新鲜度信息
type PartitionFreshnessInfo struct {
	PartitionID  int
	LastSyncTime time.Time
	Staleness    time.Duration
	Freshness    float64
	IsFresh      bool
}

// CalculateAllPartitionFreshness 计算所有分区的新鲜度
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
