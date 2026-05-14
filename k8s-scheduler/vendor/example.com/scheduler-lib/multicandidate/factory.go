// scheduler-lib/multicandidate/factory.go
//
// StrategyConfig 与 NewStrategy 提供"按名字实例化打分策略"的统一入口。
// 上层集成（k8s-scheduler 的 --parasched-strategy.name flag）通过此处
// 决定最终使用哪个 SelectionStrategy，避免把具体策略类型硬编码到集成层。
package multicandidate

import (
	"errors"
	"fmt"
)

// ErrNilStrategyConfig 表示调用 NewStrategy 时传入了 nil 配置。
var ErrNilStrategyConfig = errors.New("multicandidate: StrategyConfig is nil")

// StrategyConfig 打分策略配置（由 CLI flag / ComponentConfig 注入）。
//
// 字段语义：
//   - Name：策略名，取值为 QualityFirst / LatencyFirst / WeightedRandom /
//     QualityFirstParSync / LatencyFirstParSync 常量之一
//   - PenaltyWeight：所有策略（除 LatencyFirst 外）的 Penalty 权重 p ∈ [0,1]
//   - Seed：WeightedRandom 与 ParSync 系列策略的 PRNG seed（其他策略忽略）
type StrategyConfig struct {
	Name          string
	PenaltyWeight float64
	Seed          int64
}

// Validate 校验配置字段合法性。Name 为空或未知名会报错；PenaltyWeight 越界时
// 返回错误而非悄悄 clamp，避免 CLI 传参错误被静默吃掉。
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

// NewStrategy 根据 cfg.Name 实例化 SelectionStrategy。
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
	// Unreachable（Validate 已覆盖所有情况）。
	return nil, fmt.Errorf("multicandidate: unhandled strategy %q", cfg.Name)
}
