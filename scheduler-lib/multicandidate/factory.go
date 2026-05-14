// scheduler-lib/multicandidate/factory.go
//
// StrategyConfig and NewStrategy provide a single entry point for
// instantiating a scoring strategy by name.  The upper integration layer
// (the k8s-scheduler's --parasched-strategy.name flag) uses this to choose
// the active SelectionStrategy, avoiding hard-coding a concrete strategy type
// in the integration layer.
package multicandidate

import (
	"errors"
	"fmt"
)

// ErrNilStrategyConfig is returned by NewStrategy when a nil config is passed.
var ErrNilStrategyConfig = errors.New("multicandidate: StrategyConfig is nil")

// StrategyConfig holds scoring strategy configuration (injected via CLI flag
// or ComponentConfig).
//
// Field semantics:
//   - Name: strategy name; one of QualityFirst / LatencyFirst / WeightedRandom /
//     QualityFirstParSync / LatencyFirstParSync constants.
//   - PenaltyWeight: Penalty weight p ∈ [0,1] for all strategies except LatencyFirst.
//   - Seed: PRNG seed for WeightedRandom and ParSync-series strategies
//     (ignored by other strategies).
type StrategyConfig struct {
	Name          string
	PenaltyWeight float64
	Seed          int64
}

// Validate checks that the configuration fields are valid.  An empty or
// unknown Name is an error; an out-of-range PenaltyWeight is also an error
// (rather than silently clamping) so that CLI parameter mistakes are not
// swallowed.
func (c *StrategyConfig) Validate() error {
	if c == nil {
		return ErrNilStrategyConfig
	}
	switch c.Name {
	case QualityFirst, LatencyFirst, WeightedRandom,
		QualityFirstParSync, LatencyFirstParSync:
	case "":
		return errors.New("multicandidate: StrategyConfig.Name is empty")
	default:
		return fmt.Errorf("multicandidate: unknown strategy %q (supported: %s / %s / %s / %s / %s)",
			c.Name, QualityFirst, LatencyFirst, WeightedRandom,
			QualityFirstParSync, LatencyFirstParSync)
	}
	if c.PenaltyWeight < 0 || c.PenaltyWeight > 1 {
		return fmt.Errorf("multicandidate: PenaltyWeight=%v out of [0,1]", c.PenaltyWeight)
	}
	return nil
}

// NewStrategy instantiates a SelectionStrategy from cfg.Name.
func NewStrategy(cfg *StrategyConfig) (SelectionStrategy, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	switch cfg.Name {
	case QualityFirst:
		return NewQualityFirstStrategy(cfg.PenaltyWeight), nil
	case LatencyFirst:
		return NewLatencyFirstStrategy(), nil
	case WeightedRandom:
		return NewWeightedRandomStrategy(cfg.Seed, cfg.PenaltyWeight), nil
	case QualityFirstParSync:
		return NewQualityFirstParSyncStrategy(cfg.Seed, cfg.PenaltyWeight), nil
	case LatencyFirstParSync:
		return NewLatencyFirstParSyncStrategy(cfg.Seed, cfg.PenaltyWeight), nil
	}
	// Unreachable (Validate already covers all cases).
	return nil, fmt.Errorf("multicandidate: unhandled strategy %q", cfg.Name)
}
