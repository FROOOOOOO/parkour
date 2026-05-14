package multicandidate

import (
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// CandidateSelector 候选节点选择器
type CandidateSelector struct {
	config   *Config
	strategy SelectionStrategy

	// 可选的外部提供者
	statsProvider     types.AdoptionStatsProvider
	partitionProvider types.PartitionStateProvider

	mu sync.RWMutex
}

// NewCandidateSelector 创建选择器
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

	// 应用选项
	for _, opt := range opts {
		opt(cs)
	}

	return cs, nil
}

// SelectorOption 选择器选项
type SelectorOption func(*CandidateSelector)

// WithStrategy 设置选择策略
func WithStrategy(strategy SelectionStrategy) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.strategy = strategy
	}
}

// WithStatsProvider 设置统计提供者
func WithStatsProvider(provider types.AdoptionStatsProvider) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.statsProvider = provider
	}
}

// WithPartitionProvider 设置分区提供者
func WithPartitionProvider(provider types.PartitionStateProvider) SelectorOption {
	return func(cs *CandidateSelector) {
		cs.partitionProvider = provider
	}
}

// SelectCandidates 核心方法：选择候选节点
func (cs *CandidateSelector) SelectCandidates(adapter types.SchedulerAdapter) (*types.CandidateList, error) {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	// 1. 获取节点得分
	nodeScores := adapter.GetNodeScores()
	if len(nodeScores) == 0 {
		return nil, types.ErrNoAvailableNodes
	}

	// 2. 按得分排序
	sort.Slice(nodeScores, func(i, j int) bool {
		return nodeScores[i].Score > nodeScores[j].Score
	})

	// 3. 计算动态 K
	k := cs.calculateK(adapter, len(nodeScores))

	// 4. 得分阈值过滤
	filtered := cs.filterByScoreThreshold(nodeScores)
	if len(filtered) == 0 {
		return nil, &types.SelectionError{
			Reason:    "no nodes pass score threshold",
			NodeCount: len(nodeScores),
			Threshold: cs.config.ScoreThreshold,
		}
	}

	// 5. 构建候选列表（添加概率和分区信息）
	candidates := cs.buildCandidates(filtered, adapter)

	// 6. 使用策略选择最终候选
	selected := cs.strategy.Select(candidates, k)

	// 7. 构建返回结果
	result := &types.CandidateList{
		Candidates:    selected,
		PodKey:        adapter.GetPodKey(),
		SchedulerID:   adapter.GetSchedulerID(),
		SelectionTime: time.Now(),
	}

	return result, nil
}

// calculateK 动态计算候选数量
func (cs *CandidateSelector) calculateK(adapter types.SchedulerAdapter, availableNodes int) int {
	k := cs.config.DefaultK

	if !cs.config.EnableDynamicK {
		return cs.clampK(k, availableNodes)
	}

	// 高优先级 Pod 增加候选数
	if adapter.IsHighPriorityPod() {
		k += cs.config.HighPriorityExtraK
	}

	// 大集群增加候选数
	clusterSize := adapter.GetClusterSize()
	if clusterSize > cs.config.LargeClusterThreshold {
		k += cs.config.LargeClusterExtraK
	}

	// 根据可用节点数调整
	// 可用节点少时减少候选数，避免选择质量过差的节点
	if availableNodes < k*2 {
		k = (availableNodes + 1) / 2
	}

	return cs.clampK(k, availableNodes)
}

// clampK 限制 K 在有效范围内
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

// filterByScoreThreshold 按得分阈值过滤
func (cs *CandidateSelector) filterByScoreThreshold(nodeScores []types.NodeScore) []types.NodeScore {
	if len(nodeScores) == 0 {
		return nil
	}

	maxScore := nodeScores[0].Score
	threshold := int64(float64(maxScore) * cs.config.ScoreThreshold)

	filtered := make([]types.NodeScore, 0)
	prevScore := int64(-1)

	for _, ns := range nodeScores {
		// 得分低于阈值，停止
		if ns.Score < threshold {
			break
		}

		// 得分差异过小，停止添加（避免选择过多相似节点）
		if prevScore >= 0 && prevScore-ns.Score < cs.config.MinScoreDiff {
			// 但至少保留一个
			if len(filtered) >= cs.config.MinCandidates {
				break
			}
		}

		filtered = append(filtered, ns)
		prevScore = ns.Score
	}

	return filtered
}

// buildCandidates 构建候选节点，填充 PartitionID / Freshness / ConflictRate。
func (cs *CandidateSelector) buildCandidates(nodeScores []types.NodeScore, adapter types.SchedulerAdapter) []types.CandidateNode {
	candidates := make([]types.CandidateNode, len(nodeScores))

	for i, ns := range nodeScores {
		candidate := types.CandidateNode{
			NodeScore: ns,
			Rank:      i,
		}

		// 分区信息（LatencyFirst 使用 Freshness）。
		if cs.partitionProvider != nil {
			partitionID := cs.partitionProvider.GetPartitionID(ns.NodeName)
			candidate.PartitionID = int(partitionID)

			staleness := cs.partitionProvider.GetPartitionStaleness(partitionID)
			candidate.Freshness = cs.calculateFreshness(staleness)
		} else {
			candidate.PartitionID = -1
			candidate.Freshness = 1.0 // 无分区信息时假设新鲜
		}

		// 节点冲突率（QualityFirst / WeightedRandom 打分公式使用）。
		// provider 为 nil 或冷启动无样本时 ConflictRate=0，策略公式退化为
		// 纯 Score 排序。
		if cs.statsProvider != nil {
			candidate.ConflictRate = cs.statsProvider.GetNodeConflictRate(ns.NodeName)
		}

		candidate.Reason = fmt.Sprintf("score=%d, cr=%.2f, fresh=%.2f, part=%d",
			candidate.Score, candidate.ConflictRate, candidate.Freshness, candidate.PartitionID)

		candidates[i] = candidate
	}

	return candidates
}

// calculateFreshness 计算新鲜度 [0, 1]
func (cs *CandidateSelector) calculateFreshness(staleness time.Duration) float64 {
	// 使用指数衰减
	// freshness = exp(-staleness / tau)
	// tau = 10s 时，staleness=0 -> 1.0, staleness=10s -> 0.37, staleness=20s -> 0.14
	const tau = 10.0 // 秒

	seconds := staleness.Seconds()
	if seconds <= 0 {
		return 1.0
	}

	freshness := math.Exp(-seconds / tau)
	return freshness
}

// UpdateConfig 更新配置
func (cs *CandidateSelector) UpdateConfig(config *Config) error {
	if err := config.Validate(); err != nil {
		return err
	}

	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.config = config
	return nil
}

// SetStrategy 设置策略
func (cs *CandidateSelector) SetStrategy(strategy SelectionStrategy) {
	cs.mu.Lock()
	defer cs.mu.Unlock()

	cs.strategy = strategy
}

// GetConfig 获取当前配置
func (cs *CandidateSelector) GetConfig() *Config {
	cs.mu.RLock()
	defer cs.mu.RUnlock()

	// 返回副本
	configCopy := *cs.config
	return &configCopy
}
