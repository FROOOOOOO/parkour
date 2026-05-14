package multicandidate

import (
	"errors"
	"testing"
)

func TestNewStrategy_Dispatch(t *testing.T) {
	cases := []struct {
		cfg      StrategyConfig
		wantName string
	}{
		{StrategyConfig{Name: QualityFirst, PenaltyWeight: 0.3}, QualityFirst},
		{StrategyConfig{Name: LatencyFirst}, LatencyFirst},
		{StrategyConfig{Name: WeightedRandom, PenaltyWeight: 0.3, Seed: 42}, WeightedRandom},
	}
	for _, tc := range cases {
		t.Run(tc.cfg.Name, func(t *testing.T) {
			s, err := NewStrategy(&tc.cfg)
			if err != nil {
				t.Fatalf("NewStrategy: %v", err)
			}
			if s.Name() != tc.wantName {
				t.Errorf("Name: got %q, want %q", s.Name(), tc.wantName)
			}
		})
	}
}

func TestNewStrategy_NilConfig(t *testing.T) {
	_, err := NewStrategy(nil)
	if !errors.Is(err, ErrNilStrategyConfig) {
		t.Errorf("expected ErrNilStrategyConfig, got %v", err)
	}
}

func TestNewStrategy_EmptyName(t *testing.T) {
	_, err := NewStrategy(&StrategyConfig{})
	if err == nil {
		t.Error("expected error for empty name")
	}
}

func TestNewStrategy_UnknownName(t *testing.T) {
	_, err := NewStrategy(&StrategyConfig{Name: "Adaptive"}) // a strategy name that has been removed
	if err == nil {
		t.Error("expected error for unknown strategy name")
	}
}

func TestNewStrategy_PenaltyWeightOutOfRange(t *testing.T) {
	cases := []float64{-0.1, 1.5}
	for _, p := range cases {
		_, err := NewStrategy(&StrategyConfig{Name: QualityFirst, PenaltyWeight: p})
		if err == nil {
			t.Errorf("expected error for PenaltyWeight=%v", p)
		}
	}
}
