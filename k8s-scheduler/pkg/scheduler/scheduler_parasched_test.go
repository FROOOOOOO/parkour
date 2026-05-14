/*
Unit tests for the para-sched scheduler extension.
Tests cover: conflict penalty, multi-candidate selection, candidate annotation
writing, and pod filtering — all independent of the full scheduling framework.
*/
package scheduler

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"example.com/scheduler-lib/multicandidate"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
	"k8s.io/kubernetes/pkg/scheduler/framework"
)

// ---- helpers ----

func newTestScheduler() *Scheduler {
	return &Scheduler{
		client: fake.NewSimpleClientset(),
	}
}

// newTestSchedulerWithParaSched creates a scheduler with QualityFirst strategy.
// When `penalty` is false, PenaltyWeight is forced to 0 (equivalent to the old
// `enablePenalty=false` flag). When true, `weight` is used directly.
func newTestSchedulerWithParaSched(name string, k int, penalty bool, weight float64) *Scheduler {
	s := newTestScheduler()
	p := weight
	if !penalty {
		p = 0
	}
	s.EnableParaSched(name, k, &multicandidate.StrategyConfig{
		Name:          multicandidate.QualityFirst,
		PenaltyWeight: p,
	})
	return s
}

func makeNodeScores(names []string, scores []int64) []framework.NodePluginScores {
	result := make([]framework.NodePluginScores, len(names))
	for i, name := range names {
		result[i] = framework.NodePluginScores{
			Name:       name,
			TotalScore: scores[i],
		}
	}
	return result
}

// ---- EnableParaSched / IsParaSchedEnabled ----

func TestEnableParaSched(t *testing.T) {
	s := newTestScheduler()
	if s.IsParaSchedEnabled() {
		t.Error("expected para-sched disabled by default")
	}

	s.EnableParaSched("sched-0", 3, &multicandidate.StrategyConfig{
		Name:          multicandidate.QualityFirst,
		PenaltyWeight: 0.5,
	})
	if !s.IsParaSchedEnabled() {
		t.Error("expected para-sched enabled after EnableParaSched")
	}
	if s.parasched.name != "sched-0" {
		t.Errorf("name: got %q, want %q", s.parasched.name, "sched-0")
	}
	if s.parasched.candidateK != 3 {
		t.Errorf("candidateK: got %d, want 3", s.parasched.candidateK)
	}
	if s.parasched.strategyName != multicandidate.QualityFirst {
		t.Errorf("strategyName: got %q, want %q", s.parasched.strategyName, multicandidate.QualityFirst)
	}
}

// ---- conflict-rate penalty (via strategy layer) ----
//
// 公式：adjusted = (1-p) * normalized_score + p * (1 - conflictRate)
// 入参按 Score 降序传入 selectCandidates，strategy 按 adjusted 重排并分配
// Rank。Primary = candidates[0]。

// TestSelectCandidates_PenaltyDisabled verifies that when the penalty feature
// flag is off, candidates come out in pure Score order regardless of conflict
// rates recorded for each node.
func TestSelectCandidates_PenaltyDisabled(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 2, false, 0.5) // penalty disabled
	s.UpdateConflictRates(map[string]float64{
		"node-a": 0.9, // would be heavily penalized if enabled
		"node-b": 0.0,
		"node-c": 0.0,
	})

	scores := makeNodeScores(
		[]string{"node-a", "node-b", "node-c"},
		[]int64{100, 80, 60}, // node-a highest
	)
	candidates := s.selectCandidates(scores)

	if len(candidates) != 3 {
		t.Fatalf("len(candidates): got %d, want 3", len(candidates))
	}
	// Pure score order.
	if got, want := candidates[0].Name, "node-a"; got != want {
		t.Errorf("primary: got %q, want %q", got, want)
	}
	if got, want := candidates[1].Name, "node-b"; got != want {
		t.Errorf("secondary: got %q, want %q", got, want)
	}
}

// TestSelectCandidates_PenaltyGolden is the Phase 1 rank-order golden case
// locked against the paper Penalty formula:
//
//	adjusted = (1-p)*normScore + p*(1-conflictRate)
//
// With p=0.3, maxScore=100:
//
//	node-a: (0.7)*1.00 + (0.3)*(1-0.8) = 0.70 + 0.06 = 0.76
//	node-b: (0.7)*0.80 + (0.3)*(1-0.0) = 0.56 + 0.30 = 0.86  ← primary
//	node-c: (0.7)*0.60 + (0.3)*(1-0.0) = 0.42 + 0.30 = 0.72
//
// Expected rank: b > a > c.
func TestSelectCandidates_PenaltyGolden(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 2, true, 0.3)
	s.UpdateConflictRates(map[string]float64{
		"node-a": 0.8,
		"node-b": 0.0,
		"node-c": 0.0,
	})

	scores := makeNodeScores(
		[]string{"node-a", "node-b", "node-c"},
		[]int64{100, 80, 60},
	)
	candidates := s.selectCandidates(scores)

	expected := []string{"node-b", "node-a", "node-c"}
	if len(candidates) != len(expected) {
		t.Fatalf("len(candidates): got %d, want %d", len(candidates), len(expected))
	}
	for i, want := range expected {
		if got := candidates[i].Name; got != want {
			t.Errorf("rank %d: got %q, want %q", i, got, want)
		}
		if candidates[i].Rank != i {
			t.Errorf("rank %d: Rank field = %d, want %d", i, candidates[i].Rank, i)
		}
	}
}

// TestSelectCandidates_PenaltyUnknownNode ensures nodes without a recorded
// conflict rate default to 0 (no penalty).
func TestSelectCandidates_PenaltyUnknownNode(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 1, true, 0.5)
	// No rates set -> all defaults to 0 -> rank purely by Score.
	scores := makeNodeScores(
		[]string{"node-x", "node-y"},
		[]int64{100, 80},
	)
	candidates := s.selectCandidates(scores)
	if got, want := candidates[0].Name, "node-x"; got != want {
		t.Errorf("primary: got %q, want %q", got, want)
	}
}

// ---- multi-candidate selection ----

func TestSelectCandidates_Disabled(t *testing.T) {
	s := newTestScheduler() // no para-sched
	scores := makeNodeScores([]string{"a", "b", "c"}, []int64{100, 90, 80})
	candidates := s.selectCandidates(scores)
	// When disabled, k=0 -> should return 1 candidate (primary only)
	if len(candidates) != 1 {
		t.Fatalf("len(candidates): got %d, want 1", len(candidates))
	}
	if candidates[0].Rank != 0 {
		t.Errorf("primary rank: got %d, want 0", candidates[0].Rank)
	}
}

func TestSelectCandidates_K3(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 3, false, 0)
	scores := makeNodeScores(
		[]string{"a", "b", "c", "d", "e"},
		[]int64{50, 100, 80, 60, 90},
	)
	candidates := s.selectCandidates(scores)

	// k=3 -> should return 4 candidates (1 primary + 3 backups)
	if len(candidates) != 4 {
		t.Fatalf("len(candidates): got %d, want 4", len(candidates))
	}

	// Verify sorted by score descending.
	expectedOrder := []string{"b", "e", "c", "d"}
	for i, c := range candidates {
		if c.Name != expectedOrder[i] {
			t.Errorf("candidate[%d]: got %q, want %q", i, c.Name, expectedOrder[i])
		}
		if c.Rank != i {
			t.Errorf("candidate[%d].Rank: got %d, want %d", i, c.Rank, i)
		}
	}
}

func TestSelectCandidates_MoreKThanNodes(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 10, false, 0)
	scores := makeNodeScores([]string{"a", "b"}, []int64{100, 90})
	candidates := s.selectCandidates(scores)
	// k=10 but only 2 nodes -> should return 2
	if len(candidates) != 2 {
		t.Fatalf("len(candidates): got %d, want 2", len(candidates))
	}
}

// ---- write candidate annotation ----

func TestWriteCandidateAnnotation(t *testing.T) {
	fakeClient := fake.NewSimpleClientset()

	// Pre-create the pod so Patch succeeds.
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "test-pod", Namespace: "default"},
	}
	_, err := fakeClient.CoreV1().Pods("default").Create(context.Background(), pod, metav1.CreateOptions{})
	if err != nil {
		t.Fatalf("create pod: %v", err)
	}

	s := &Scheduler{client: fakeClient}
	s.EnableParaSched("sched-0", 3, &multicandidate.StrategyConfig{
		Name: multicandidate.QualityFirst,
	})

	candidates := []CandidateNode{
		{Name: "node-a", Score: 100, Rank: 0},
		{Name: "node-b", Score: 90, Rank: 1},
		{Name: "node-c", Score: 80, Rank: 2},
	}

	err = s.writeCandidateAnnotation(context.Background(), pod, candidates)
	if err != nil {
		t.Fatalf("writeCandidateAnnotation failed: %v", err)
	}

	// Verify the annotation was written.
	patched, err := fakeClient.CoreV1().Pods("default").Get(context.Background(), "test-pod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get pod: %v", err)
	}

	raw, ok := patched.Annotations[candidateNodesAnnotationKey]
	if !ok {
		t.Fatal("candidate annotation not found")
	}

	var ann candidateAnnotation
	if err := json.Unmarshal([]byte(raw), &ann); err != nil {
		t.Fatalf("unmarshal annotation: %v", err)
	}

	if len(ann.Candidates) != 3 {
		t.Fatalf("candidates count: got %d, want 3", len(ann.Candidates))
	}
	if ann.Candidates[0].Name != "node-a" {
		t.Errorf("primary candidate: got %q, want %q", ann.Candidates[0].Name, "node-a")
	}
	if ann.Scheduler != "sched-0" {
		t.Errorf("scheduler: got %q, want %q", ann.Scheduler, "sched-0")
	}
	if ann.PodKey != "default/test-pod" {
		t.Errorf("podKey: got %q, want %q", ann.PodKey, "default/test-pod")
	}
}

func TestWriteCandidateAnnotation_PatchFailure(t *testing.T) {
	fakeClient := fake.NewSimpleClientset()
	fakeClient.PrependReactor("patch", "pods", func(action k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, fmt.Errorf("simulated API error")
	})

	s := &Scheduler{client: fakeClient}
	s.EnableParaSched("sched-0", 3, &multicandidate.StrategyConfig{
		Name: multicandidate.QualityFirst,
	})

	err := s.writeCandidateAnnotation(context.Background(),
		&v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: "ns"}},
		[]CandidateNode{{Name: "n", Score: 1, Rank: 0}})
	if err == nil {
		t.Error("expected error on patch failure")
	}
}

// ---- pod filter ----

func TestParaSchedPodFilter(t *testing.T) {
	s := newTestSchedulerWithParaSched("sched-0", 3, false, 0)

	tests := []struct {
		name   string
		pod    *v1.Pod
		expect bool
	}{
		{
			name: "assigned to this scheduler",
			pod: &v1.Pod{ObjectMeta: metav1.ObjectMeta{
				Annotations: map[string]string{schedulerNameAnnotationKey: "sched-0"},
			}},
			expect: true,
		},
		{
			name: "assigned to different scheduler",
			pod: &v1.Pod{ObjectMeta: metav1.ObjectMeta{
				Annotations: map[string]string{schedulerNameAnnotationKey: "sched-1"},
			}},
			expect: false,
		},
		{
			name:   "no annotation",
			pod:    &v1.Pod{},
			expect: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := s.paraSchedPodFilter(tt.pod)
			if got != tt.expect {
				t.Errorf("paraSchedPodFilter() = %v, want %v", got, tt.expect)
			}
		})
	}
}

func TestParaSchedPodFilter_Disabled(t *testing.T) {
	s := newTestScheduler() // no para-sched
	pod := &v1.Pod{ObjectMeta: metav1.ObjectMeta{
		Annotations: map[string]string{schedulerNameAnnotationKey: "sched-0"},
	}}
	if s.paraSchedPodFilter(pod) {
		t.Error("should return false when para-sched is disabled")
	}
}

// ---- UpdateConflictRates ----

func TestUpdateConflictRates(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 3, true, 0.5)

	s.UpdateConflictRates(map[string]float64{
		"node-a": 0.3,
		"node-b": 0.7,
	})

	if cr := s.getConflictRate("node-a"); cr != 0.3 {
		t.Errorf("node-a conflict rate: got %f, want 0.3", cr)
	}
	if cr := s.getConflictRate("node-b"); cr != 0.7 {
		t.Errorf("node-b conflict rate: got %f, want 0.7", cr)
	}
	if cr := s.getConflictRate("unknown"); cr != 0 {
		t.Errorf("unknown node conflict rate: got %f, want 0", cr)
	}
}

func TestUpdateConflictRates_Disabled(t *testing.T) {
	s := newTestScheduler()
	// Should not panic.
	s.UpdateConflictRates(map[string]float64{"a": 0.5})
	if cr := s.getConflictRate("a"); cr != 0 {
		t.Errorf("conflict rate should be 0 when disabled: got %f", cr)
	}
}

// ---- integration: full-penalty (p=1) order ----

// TestSelectCandidates_PenaltyFull verifies that with p=1 the strategy orders
// purely by (1 - conflictRate), ignoring Score. Formula:
//
//	adjusted = 0*normScore + 1*(1-conflictRate) = 1 - conflictRate
func TestSelectCandidates_PenaltyFull(t *testing.T) {
	s := newTestSchedulerWithParaSched("s0", 2, true, 1.0) // full penalty weight
	s.UpdateConflictRates(map[string]float64{
		"node-a": 0.8, // 1-0.8 = 0.2
		"node-b": 0.0, // 1-0.0 = 1.0 ← primary
		"node-c": 0.0, // 1-0.0 = 1.0
	})

	// node-a has the highest raw Score but the highest conflict rate.
	scores := makeNodeScores(
		[]string{"node-a", "node-b", "node-c"},
		[]int64{100, 80, 70},
	)
	candidates := s.selectCandidates(scores)

	// node-b / node-c tied at adjusted=1.0. stable sort preserves the input
	// order (node-b ahead of node-c by Score); node-a last.
	if candidates[0].Name != "node-b" {
		t.Errorf("p=1 primary: got %q, want %q", candidates[0].Name, "node-b")
	}
	if candidates[2].Name != "node-a" {
		t.Errorf("p=1 last: got %q, want %q (highest conflict)", candidates[2].Name, "node-a")
	}
}

// ---- candidateAnnotation JSON round-trip ----

func TestCandidateAnnotationJSON(t *testing.T) {
	ann := candidateAnnotation{
		Candidates: []CandidateNode{
			{Name: "node-a", Score: 100, Rank: 0},
			{Name: "node-b", Score: 90, Rank: 1},
		},
		PodKey:    "default/pod-0",
		Scheduler: "sched-0",
		Timestamp: time.Now().Truncate(time.Millisecond),
	}

	data, err := json.Marshal(ann)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded candidateAnnotation
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if len(decoded.Candidates) != 2 {
		t.Fatalf("candidates: got %d, want 2", len(decoded.Candidates))
	}
	if decoded.Candidates[0].Name != "node-a" {
		t.Errorf("first candidate: got %q, want %q", decoded.Candidates[0].Name, "node-a")
	}
	if decoded.Scheduler != "sched-0" {
		t.Errorf("scheduler: got %q, want %q", decoded.Scheduler, "sched-0")
	}
}
