package stats

// ProbabilityCalculator 概率计算器。
//
// Phase 2 精简：原本为本地状态更新策略（已废弃）与早期 Penalty 公式预留的
// CalculateAdoptionProbability / CalculateUpdateProbability / ExponentialDecay /
// NormalizeProbabilities / Entropy 等函数在 strategy 迁移到论文公式
// `(1-p)*normScore + p*(1-conflictRate)` 后不再被引用，已删除。
// 当前仅保留 CalculateConflictRate，供 AdoptionStatsCache.updateSummary 及
// AdoptionStats CRD watcher 复用。
type ProbabilityCalculator struct{}

// NewProbabilityCalculator 创建概率计算器
func NewProbabilityCalculator() *ProbabilityCalculator {
	return &ProbabilityCalculator{}
}

// CalculateConflictRate 计算累计冲突率 = failure / (success + failure)。
// successCount+failureCount=0 时返回 0（冷启动不产生伪数据）。
func (pc *ProbabilityCalculator) CalculateConflictRate(successCount, failureCount int64) float64 {
	total := successCount + failureCount
	if total == 0 {
		return 0
	}
	return float64(failureCount) / float64(total)
}
