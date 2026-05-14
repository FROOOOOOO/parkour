package multicandidate

import "fmt"

// Config 候选选择器配置
type Config struct {
	// === 基础配置 ===

	// DefaultK 默认候选数量
	DefaultK int `yaml:"defaultK"`

	// MinCandidates 最小候选数
	MinCandidates int `yaml:"minCandidates"`

	// MaxCandidates 最大候选数
	MaxCandidates int `yaml:"maxCandidates"`

	// === 得分过滤 ===

	// ScoreThreshold 得分阈值 (0-1)
	// 只选择得分 >= 最高分 * ScoreThreshold 的节点
	ScoreThreshold float64 `yaml:"scoreThreshold"`

	// MinScoreDiff 最小得分差异
	// 候选之间得分差异小于此值时停止添加
	MinScoreDiff int64 `yaml:"minScoreDiff"`

	// === Penalty 配置 ===

	// PenaltyWeight Penalty 权重 p ∈ [0,1]，对应论文 Penalty 公式：
	//   adjusted = (1-p) * normalized_score + p * (1 - conflictRate)
	// p=0 时退化为纯按 Score 排序；p=1 时纯按 (1-conflictRate) 排序。
	PenaltyWeight float64 `yaml:"penaltyWeight"`

	// === 动态调整 ===

	// EnableDynamicK 是否启用动态 K 调整
	EnableDynamicK bool `yaml:"enableDynamicK"`

	// HighPriorityExtraK 高优先级 Pod 额外候选数
	HighPriorityExtraK int `yaml:"highPriorityExtraK"`

	// LargeClusterThreshold 大集群阈值
	LargeClusterThreshold int `yaml:"largeClusterThreshold"`

	// LargeClusterExtraK 大集群额外候选数
	LargeClusterExtraK int `yaml:"largeClusterExtraK"`
}

// DefaultConfig 默认配置
func DefaultConfig() *Config {
	return &Config{
		DefaultK:              3,
		MinCandidates:         1,
		MaxCandidates:         10,
		ScoreThreshold:        0.85,
		MinScoreDiff:          2,
		PenaltyWeight:         0.3,
		EnableDynamicK:        true,
		HighPriorityExtraK:    2,
		LargeClusterThreshold: 5000,
		LargeClusterExtraK:    2,
	}
}

// Validate 验证配置
func (c *Config) Validate() error {
	if c.DefaultK < 1 {
		return fmt.Errorf("defaultK must be >= 1")
	}
	if c.MinCandidates < 1 {
		return fmt.Errorf("minCandidates must be >= 1")
	}
	if c.MaxCandidates < c.MinCandidates {
		return fmt.Errorf("maxCandidates must be >= minCandidates")
	}
	if c.ScoreThreshold < 0 || c.ScoreThreshold > 1 {
		return fmt.Errorf("scoreThreshold must be in [0, 1]")
	}
	if c.MinScoreDiff < 0 || c.MinScoreDiff > 100 {
		return fmt.Errorf("minScoreDiff must be in [0, 100]")
	}
	if c.PenaltyWeight < 0 || c.PenaltyWeight > 1 {
		return fmt.Errorf("penaltyWeight must be in [0, 1]")
	}
	if c.HighPriorityExtraK < 0 {
		return fmt.Errorf("highPriorityExtraK must be >= 0")
	}
	if c.LargeClusterThreshold < 0 {
		return fmt.Errorf("largeClusterThreshold must be >= 0")
	}
	if c.LargeClusterExtraK < 0 {
		return fmt.Errorf("largeClusterExtraK must be >= 0")
	}
	return nil
}
