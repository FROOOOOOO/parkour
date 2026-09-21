// Package binder implements a simplified Binder that reads candidate node lists
// from pod annotations, performs conflict checking with fallback, and binds pods.
package binder

import (
	v1 "k8s.io/api/core/v1"

	"example.com/para-scheduler/pkg/cache"
)

// FeasibilityRule decides whether a pod may be admitted to a candidate node at
// commit time.
//
// Rules are evaluated against the Binder's own running account of admitted
// requests. That account reflects every binding the Binder has committed,
// including those still in flight, which is state the schedulers' local caches
// do not have when they score. A rule is therefore the last chance to reject a
// placement before it reaches the data plane.
//
// The same rule instance is shared by all Binder workers, so implementations
// must be safe for concurrent use and must not mutate the pod or the node info
// they are given.
type FeasibilityRule interface {
	// Name returns a short stable identifier for the rule, used in logs.
	Name() string

	// Check reports whether pod may be placed on the node described by nodeInfo.
	// Returns (true, "") when the pod is admissible, or (false, reason) with a
	// short human-readable reason otherwise. The reason is attached to the
	// per-candidate failure record and surfaced in logs and binding results.
	Check(pod *v1.Pod, nodeInfo *cache.NodeInfo) (bool, string)
}

// ResourceFitRule is the default feasibility rule. It compares the pod's
// aggregate CPU and memory requests against the node's unallocated capacity as
// tracked by the Binder cache.
//
// It deliberately does not re-run the scheduler's Filter plugins: affinity,
// taints and topology are evaluated once at scheduling time and do not change
// as other schedulers commit, whereas remaining capacity does.
type ResourceFitRule struct{}

// Name implements FeasibilityRule.
func (ResourceFitRule) Name() string { return "ResourceFit" }

// Check implements FeasibilityRule by comparing the pod's computed request
// against the node's remaining allocatable CPU and memory.
func (ResourceFitRule) Check(pod *v1.Pod, nodeInfo *cache.NodeInfo) (bool, string) {
	return cache.FitsNode(cache.ComputePodRequest(pod), nodeInfo)
}

// FeasibilityRules is an ordered chain of rules. The first rule that rejects a
// node short-circuits the chain, so cheaper rules should be placed first.
type FeasibilityRules []FeasibilityRule

// defaultRules is the chain used when no explicit configuration is supplied.
// ResourceFitRule is stateless, so a single shared instance is safe.
var defaultRules = FeasibilityRules{ResourceFitRule{}}

// DefaultFeasibilityRules returns the rule chain the Binder uses unless
// SetFeasibilityRules overrides it. The result is a copy, so callers may append
// to it without affecting other Binders.
func DefaultFeasibilityRules() FeasibilityRules {
	return append(FeasibilityRules(nil), defaultRules...)
}

// Check evaluates every rule in order and returns the first rejection.
//
// An empty chain fails closed — it rejects the node rather than admitting it —
// so a Binder that was never configured stops binding loudly instead of
// silently committing without any commit-time conflict detection. Disabling the
// check is therefore an explicit choice rather than a consequence of a zero
// value.
func (rs FeasibilityRules) Check(pod *v1.Pod, nodeInfo *cache.NodeInfo) (bool, string) {
	if len(rs) == 0 {
		return false, "no feasibility rules configured"
	}
	for _, r := range rs {
		if ok, reason := r.Check(pod, nodeInfo); !ok {
			return false, reason
		}
	}
	return true, ""
}
