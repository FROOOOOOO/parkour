package binder

import (
	"testing"

	v1 "k8s.io/api/core/v1"

	"example.com/para-scheduler/pkg/cache"
)

// ---- default rule: resource fit ----

func TestResourceFitRule_Fits(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 1000, Memory: 2 * 1024 * 1024 * 1024},
	}
	pod := makePod("pod-0", "default", 500, 1024*1024)
	fits, reason := ResourceFitRule{}.Check(pod, ni)
	if !fits {
		t.Errorf("expected pod to fit, reason: %s", reason)
	}
}

func TestResourceFitRule_InsufficientCPU(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 3800, Memory: 0},
	}
	pod := makePod("pod-0", "default", 500, 1024)
	fits, reason := ResourceFitRule{}.Check(pod, ni)
	if fits {
		t.Error("expected pod not to fit due to CPU")
	}
	if reason == "" {
		t.Error("expected non-empty reason")
	}
}

func TestResourceFitRule_InsufficientMemory(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 0, Memory: 8 * 1024 * 1024 * 1024},
	}
	pod := makePod("pod-0", "default", 100, 1024*1024)
	fits, _ := ResourceFitRule{}.Check(pod, ni)
	if fits {
		t.Error("expected pod not to fit due to memory")
	}
}

// The default chain must be exactly the resource-fit rule, so a Binder built by
// NewBinder behaves as documented without further configuration.
func TestDefaultFeasibilityRules_IsResourceFit(t *testing.T) {
	rules := DefaultFeasibilityRules()
	if len(rules) != 1 {
		t.Fatalf("expected 1 default rule, got %d", len(rules))
	}
	if _, ok := rules[0].(ResourceFitRule); !ok {
		t.Errorf("expected default rule to be ResourceFitRule, got %T", rules[0])
	}
	if got := rules[0].Name(); got != "ResourceFit" {
		t.Errorf("expected rule name %q, got %q", "ResourceFit", got)
	}
}

// ---- feasibility rule chain ----

// rejectRule is a test rule that always rejects, recording whether it ran.
type rejectRule struct {
	name   string
	called *int
}

func (r rejectRule) Name() string { return r.name }

func (r rejectRule) Check(*v1.Pod, *cache.NodeInfo) (bool, string) {
	*r.called++
	return false, r.name + "-rejected"
}

// admitRule is a test rule that always admits, recording whether it ran.
type admitRule struct {
	name   string
	called *int
}

func (r admitRule) Name() string { return r.name }

func (r admitRule) Check(*v1.Pod, *cache.NodeInfo) (bool, string) {
	*r.called++
	return true, ""
}

func roomyNode() *cache.NodeInfo {
	return &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
	}
}

// An empty chain must fail closed, so an unconfigured Binder stops binding
// loudly instead of committing without any feasibility check.
func TestFeasibilityRules_EmptyChainFailsClosed(t *testing.T) {
	fits, reason := FeasibilityRules{}.Check(makePod("pod-0", "default", 100, 1024), roomyNode())
	if fits {
		t.Error("expected empty chain to reject")
	}
	if reason == "" {
		t.Error("expected non-empty reason")
	}
}

// The first rejection short-circuits: later rules must not run.
func TestFeasibilityRules_ShortCircuitsOnFirstRejection(t *testing.T) {
	var firstCalls, secondCalls int
	rules := FeasibilityRules{
		rejectRule{name: "first", called: &firstCalls},
		admitRule{name: "second", called: &secondCalls},
	}

	fits, reason := rules.Check(makePod("pod-0", "default", 100, 1024), roomyNode())
	if fits {
		t.Error("expected chain to reject")
	}
	if reason != "first-rejected" {
		t.Errorf("expected the first rule's reason, got %q", reason)
	}
	if firstCalls != 1 {
		t.Errorf("expected first rule to run once, ran %d times", firstCalls)
	}
	if secondCalls != 0 {
		t.Errorf("expected second rule to be skipped, ran %d times", secondCalls)
	}
}

// A node is admitted only when every rule in the chain admits it.
func TestFeasibilityRules_AdmitsWhenAllRulesPass(t *testing.T) {
	var firstCalls, secondCalls int
	rules := FeasibilityRules{
		admitRule{name: "first", called: &firstCalls},
		admitRule{name: "second", called: &secondCalls},
	}

	fits, reason := rules.Check(makePod("pod-0", "default", 100, 1024), roomyNode())
	if !fits {
		t.Errorf("expected chain to admit, reason: %s", reason)
	}
	if firstCalls != 1 || secondCalls != 1 {
		t.Errorf("expected both rules to run once, got %d and %d", firstCalls, secondCalls)
	}
}

// DefaultFeasibilityRules must hand out an independent copy, so appending to
// one Binder's chain cannot alter another's.
func TestDefaultFeasibilityRules_ReturnsIndependentCopy(t *testing.T) {
	var calls int
	extended := append(DefaultFeasibilityRules(), rejectRule{name: "extra", called: &calls})

	if got := len(DefaultFeasibilityRules()); got != 1 {
		t.Errorf("default chain was mutated: expected 1 rule, got %d", got)
	}
	if got := len(extended); got != 2 {
		t.Errorf("expected extended chain to hold 2 rules, got %d", got)
	}
}
