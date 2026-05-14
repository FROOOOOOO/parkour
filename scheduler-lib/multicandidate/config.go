package multicandidate

import "fmt"

// Config holds configuration for the candidate selector.
type Config struct {
	// === Basic configuration ===

	// DefaultK is the default number of candidates.
	DefaultK int `yaml:"defaultK"`

	// MinCandidates is the minimum number of candidates.
	MinCandidates int `yaml:"minCandidates"`

	// MaxCandidates is the maximum number of candidates.
	MaxCandidates int `yaml:"maxCandidates"`

	// === Score filtering ===

	// ScoreThreshold is the score threshold in [0, 1].
	// Only nodes with score >= maxScore * ScoreThreshold are considered.
	ScoreThreshold float64 `yaml:"scoreThreshold"`

	// MinScoreDiff is the minimum score difference between consecutive candidates.
	// Candidates are no longer added when the gap drops below this value.
	MinScoreDiff int64 `yaml:"minScoreDiff"`

	// === Penalty configuration ===

	// PenaltyWeight is the Penalty weight p ∈ [0,1], corresponding to the
	// paper's Penalty formula:
	//   adjusted = (1-p) * normalized_score + p * (1 - conflictRate)
	// p=0 degrades to pure Score ordering; p=1 orders purely by (1-conflictRate).
	PenaltyWeight float64 `yaml:"penaltyWeight"`

	// === Dynamic adjustment ===

	// EnableDynamicK controls whether dynamic K adjustment is enabled.
	EnableDynamicK bool `yaml:"enableDynamicK"`

	// HighPriorityExtraK is the extra candidate count added for high-priority pods.
	HighPriorityExtraK int `yaml:"highPriorityExtraK"`

	// LargeClusterThreshold is the cluster size above which large-cluster mode activates.
	LargeClusterThreshold int `yaml:"largeClusterThreshold"`

	// LargeClusterExtraK is the extra candidate count added in large-cluster mode.
	LargeClusterExtraK int `yaml:"largeClusterExtraK"`
}

// DefaultConfig returns a Config with sensible defaults.
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

// Validate checks configuration field validity.
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
