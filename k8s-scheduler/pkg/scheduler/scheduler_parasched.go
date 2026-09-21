/*
Package scheduler extension for para-sched: multi-candidate selection,
conflict-rate penalty, and candidate-annotation binding.

When paraSchedConfig is nil (default), all behavior is identical to the
original kube-scheduler. The extension is activated by calling
Scheduler.EnableParaSched() before Run().
*/
package scheduler

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"example.com/scheduler-lib/multicandidate"
	libstats "example.com/scheduler-lib/stats"
	libtypes "example.com/scheduler-lib/types"
	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"k8s.io/klog/v2"
	"k8s.io/kubernetes/pkg/scheduler/framework"
	schedmetrics "k8s.io/kubernetes/pkg/scheduler/metrics"
)

// ---- patch error classification ----

// maxConflictRetries bounds the number of retries performed internally in
// writeCandidateAnnotation when the API server returns 409 Conflict
// (typically triggered by concurrent updates from kubelet/other controllers
// on the pod's annotations or status).
const maxConflictRetries = 3

// isPermanentWriteError returns true for HTTP status codes that will not
// succeed by retrying: Unauthorized, Forbidden, UnprocessableEntity
// (which includes admission-webhook rejections). NotFound is treated
// separately by the caller because the pod no longer exists and the
// cleanup path differs from a permanent-but-live-pod failure.
func isPermanentWriteError(err error) bool {
	return apierrors.IsUnauthorized(err) ||
		apierrors.IsForbidden(err) ||
		apierrors.IsInvalid(err) ||
		apierrors.IsMethodNotSupported(err)
}

// ---- annotation keys (must match para-scheduler/pkg/annotation) ----

const (
	candidateNodesAnnotationKey = "para-scheduler.io/candidate-nodes"
	schedulerNameAnnotationKey  = "para-scheduler.io/scheduler-name"
)

// ---- candidate types ----

// CandidateNode represents a candidate node with its score and rank.
type CandidateNode struct {
	Name        string `json:"node"`
	Score       int64  `json:"score"`
	Rank        int    `json:"rank"`
	PartitionID int    `json:"partitionID,omitempty"`
}

// candidateAnnotation is the JSON structure written to the pod annotation.
type candidateAnnotation struct {
	Candidates []CandidateNode `json:"candidates"`
	PodKey     string          `json:"podKey"`
	Scheduler  string          `json:"scheduler"`
	Timestamp  time.Time       `json:"ts"`
}

// ---- para-sched configuration ----

// paraSchedConfig holds all para-sched extension state.
// A nil *paraSchedConfig means para-sched is disabled.
type paraSchedConfig struct {
	// Instance name assigned by Dispatcher (e.g. "sched-0").
	name string

	// Feature toggles.
	enableMultiCandidate bool
	candidateK           int     // number of backup candidates
	penaltyWeight        float64 // p in [0,1] of the penalty formula

	// Strategy name (QualityFirst / LatencyFirst / WeightedRandom).
	strategyName string

	// scheduler-lib components (single source of truth for algorithms).
	candidateSelector *multicandidate.CandidateSelector
	probCalculator    *libstats.ProbabilityCalculator

	// Stats provider wires AdoptionStats CRD data (per-node conflict rates)
	// into the CandidateSelector strategy. Scheduler-side is read-only:
	// UpdateResult is a no-op.
	statsProvider *k8sStatsProvider

	// ParSync consumer side.
	enableParSync bool

	// Per-partition last-sync timestamps. Used by k8sPartitionProvider to
	// derive per-node Freshness for the LatencyFirst strategy. Populated by
	// UpdatePartitionSyncTime (called from parasched_sync.applyPendingSnapshot).
	partitionSyncMu    sync.RWMutex
	partitionSyncTimes map[int]time.Time // partitionID -> last applied timestamp
}

// k8sStatsProvider implements libtypes.AdoptionStatsProvider on the scheduler
// side. It holds a per-node conflict-rate map fed from the AdoptionStats CRD
// watcher in parasched_sync.go.
//
// Scheduler is a read-only consumer: UpdateResult is a no-op and GetStats
// returns nil.
type k8sStatsProvider struct {
	mu            sync.RWMutex
	conflictRates map[string]float64 // nodeName -> conflict rate [0,1]
}

func newK8sStatsProvider() *k8sStatsProvider {
	return &k8sStatsProvider{
		conflictRates: make(map[string]float64),
	}
}

// SetConflictRates replaces the per-node conflict-rate map. Copy-on-write
// semantics: callers pass a fully-formed map; we take ownership.
func (p *k8sStatsProvider) SetConflictRates(rates map[string]float64) {
	if rates == nil {
		rates = make(map[string]float64)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.conflictRates = rates
}

// GetNodeConflictRate implements AdoptionStatsProvider.
func (p *k8sStatsProvider) GetNodeConflictRate(nodeName string) float64 {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.conflictRates[nodeName]
}

// UpdateResult implements AdoptionStatsProvider (scheduler-side is read-only).
func (p *k8sStatsProvider) UpdateResult(libtypes.BindingResult) {}

// GetStats implements AdoptionStatsProvider.
func (p *k8sStatsProvider) GetStats() *libtypes.AdoptionStats { return nil }

// k8sPartitionProvider implements libtypes.PartitionStateProvider on the
// scheduler side. It answers partition queries for the CandidateSelector
// (primarily to feed Freshness into LatencyFirstStrategy).
//
// Data sources:
//   - GetPartitionID: node label "para-scheduler.io/partition-id"
//     (read via the scheduler's nodeInfo snapshot)
//   - GetPartitionStaleness: time.Since(parasched.partitionSyncTimes[pid]);
//     if a partition has never been synced (empty map entry), returns 0 so
//     the selector's calculateFreshness treats it as "fresh by default"
//     (optimistic cold-start behavior)
//
// GetFreshPartitions / GetPartitionInfo are unused by the strategies we ship
// (QualityFirst / LatencyFirst / WeightedRandom); stubs return nil/empty.
type k8sPartitionProvider struct {
	sched *Scheduler
}

func newK8sPartitionProvider(sched *Scheduler) *k8sPartitionProvider {
	return &k8sPartitionProvider{sched: sched}
}

func (p *k8sPartitionProvider) GetPartitionID(nodeName string) int {
	return getNodePartitionID2(p.sched, nodeName)
}

func (p *k8sPartitionProvider) GetPartitionStaleness(partitionID int) time.Duration {
	if p.sched.parasched == nil {
		return 0
	}
	p.sched.parasched.partitionSyncMu.RLock()
	lastSync, ok := p.sched.parasched.partitionSyncTimes[partitionID]
	p.sched.parasched.partitionSyncMu.RUnlock()
	if !ok {
		return 0
	}
	return time.Since(lastSync)
}

func (p *k8sPartitionProvider) GetFreshPartitions() []int { return nil }

func (p *k8sPartitionProvider) GetPartitionInfo(partitionID int) *libtypes.PartitionInfo {
	return nil
}

// EnableParaSched activates para-sched mode on the scheduler.
// Must be called before Run(). When enabled:
//   - Pod filtering uses the scheduler-name annotation instead of pod.Spec.SchedulerName
//   - Binding writes a candidate-annotation instead of calling the Bind API
//   - Score ranking is fully owned by the scheduler-lib strategy (QualityFirst /
//     LatencyFirst / WeightedRandom) configured via strategyCfg
//   - Multiple candidate nodes are selected (if candidateK > 0)
//
// strategyCfg is required; an invalid configuration is a fatal misconfiguration
// and causes klog.Fatal so the scheduler refuses to start rather than silently
// falling back to a different strategy.
func (sched *Scheduler) EnableParaSched(name string, candidateK int, strategyCfg *multicandidate.StrategyConfig) {
	if strategyCfg == nil {
		klog.Fatal("EnableParaSched: strategyCfg is nil")
	}
	strategy, err := multicandidate.NewStrategy(strategyCfg)
	if err != nil {
		klog.Fatalf("EnableParaSched: invalid strategy config: %v", err)
	}

	// Providers surface ParSync state + AdoptionStats CRD data to the strategy.
	statsProvider := newK8sStatsProvider()
	partitionProvider := newK8sPartitionProvider(sched)

	// Initialize scheduler-lib CandidateSelector with config matching k8s params.
	selectorCfg := &multicandidate.Config{
		DefaultK:       candidateK + 1, // K backups + 1 primary
		MinCandidates:  1,
		MaxCandidates:  candidateK + 1,
		ScoreThreshold: 0, // no threshold filtering — k8s Filter phase already does this
		MinScoreDiff:   0,
		PenaltyWeight:  strategyCfg.PenaltyWeight,
	}
	selector, err := multicandidate.NewCandidateSelector(selectorCfg,
		multicandidate.WithStrategy(strategy),
		multicandidate.WithStatsProvider(statsProvider),
		multicandidate.WithPartitionProvider(partitionProvider),
	)
	if err != nil {
		klog.Fatalf("EnableParaSched: CandidateSelector construction failed: %v", err)
	}

	sched.parasched = &paraSchedConfig{
		name:                 name,
		enableMultiCandidate: candidateK > 0,
		candidateK:           candidateK,
		penaltyWeight:        strategyCfg.PenaltyWeight,
		strategyName:         strategyCfg.Name,
		candidateSelector:    selector,
		probCalculator:       libstats.NewProbabilityCalculator(),
		statsProvider:        statsProvider,
		// partitionSyncTimes is allocated here so that GetPartitionStaleness
		// works before EnableParaSync is called (returns 0 = cold-start fresh).
		partitionSyncTimes: make(map[int]time.Time),
	}
}

// IsParaSchedEnabled returns true if para-sched mode is active.
func (sched *Scheduler) IsParaSchedEnabled() bool {
	return sched.parasched != nil
}

// ---- conflict rate management ----

// UpdateConflictRates bulk-updates conflict rates (called from external CRD watcher).
// Delegates to the stats provider; when para-sched is disabled this is a no-op.
func (sched *Scheduler) UpdateConflictRates(rates map[string]float64) {
	if sched.parasched == nil || sched.parasched.statsProvider == nil {
		return
	}
	sched.parasched.statsProvider.SetConflictRates(rates)
}

// getConflictRate returns the conflict rate for a node, defaulting to 0.
// Preserved for backward-compatible access in tests and diagnostics; strategy
// scoring now consumes conflict rates through the stats provider directly.
func (sched *Scheduler) getConflictRate(nodeName string) float64 {
	if sched.parasched == nil || sched.parasched.statsProvider == nil {
		return 0
	}
	return sched.parasched.statsProvider.GetNodeConflictRate(nodeName)
}

// ---- multi-candidate selection ----

// k8sSchedulerAdapter adapts k8s framework scoring data to the
// scheduler-lib SchedulerAdapter interface. This is a per-call value
// object — cheap to create.
type k8sSchedulerAdapter struct {
	scores      []framework.NodePluginScores
	podKey      string
	schedulerID string
	clusterSize int
	highPri     bool
}

func (a *k8sSchedulerAdapter) GetNodeScores() []libtypes.NodeScore {
	out := make([]libtypes.NodeScore, len(a.scores))
	for i, s := range a.scores {
		out[i] = libtypes.NodeScore{NodeName: s.Name, Score: s.TotalScore}
	}
	return out
}
func (a *k8sSchedulerAdapter) GetClusterSize() int       { return a.clusterSize }
func (a *k8sSchedulerAdapter) GetSchedulerID() int       { return 0 }
func (a *k8sSchedulerAdapter) IsHighPriorityPod() bool   { return a.highPri }
func (a *k8sSchedulerAdapter) GetPodKey() string          { return a.podKey }

// selectCandidates delegates to scheduler-lib CandidateSelector.
// Returns the primary (rank=0) and K backup candidates.
func (sched *Scheduler) selectCandidates(scores []framework.NodePluginScores) []CandidateNode {
	start := time.Now()
	defer func() {
		schedmetrics.ParaSchedCandidateSelectionDuration.Observe(schedmetrics.SinceInSeconds(start))
	}()

	if sched.parasched == nil || sched.parasched.candidateSelector == nil {
		// Fallback: just return the first node.
		if len(scores) == 0 {
			return nil
		}
		return []CandidateNode{{Name: scores[0].Name, Score: scores[0].TotalScore, Rank: 0}}
	}

	adapter := &k8sSchedulerAdapter{
		scores:      scores,
		clusterSize: len(scores),
	}

	result, err := sched.parasched.candidateSelector.SelectCandidates(adapter)
	if err != nil {
		klog.Errorf("CandidateSelector.SelectCandidates failed: %v, using top node", err)
		if len(scores) > 0 {
			return []CandidateNode{{Name: scores[0].Name, Score: scores[0].TotalScore, Rank: 0}}
		}
		return nil
	}

	// Convert scheduler-lib CandidateNode -> local CandidateNode.
	// Populate PartitionID from node labels (scheduler-lib doesn't have
	// access to K8s node objects, so we fill it here).
	candidates := make([]CandidateNode, len(result.Candidates))
	for i, c := range result.Candidates {
		candidates[i] = CandidateNode{
			Name:        c.NodeName,
			Score:       c.Score,
			Rank:        c.Rank,
			PartitionID: getNodePartitionID2(sched, c.NodeName),
		}
	}

	// Record the primary candidate's score.
	if len(candidates) > 0 {
		schedmetrics.ParaSchedSelectedNodeScore.Observe(float64(candidates[0].Score))
	}

	return candidates
}

// ---- binding replacement: write candidate annotation ----

// writeCandidateAnnotation patches the pod with a candidate-nodes annotation
// instead of calling the Bind API. The Binder component will watch for this
// annotation and take over the actual binding.
//
// Retry behavior: 409 Conflict is retried up to maxConflictRetries times with
// a short backoff. All other errors are returned unchanged so the caller can
// classify them (NotFound / permanent / transient) and pick the correct
// cleanup path.
func (sched *Scheduler) writeCandidateAnnotation(ctx context.Context, pod *v1.Pod, candidates []CandidateNode) error {
	ann := candidateAnnotation{
		Candidates: candidates,
		PodKey:     pod.Namespace + "/" + pod.Name,
		Scheduler:  sched.parasched.name,
		Timestamp:  time.Now(),
	}
	annJSON, err := json.Marshal(ann)
	if err != nil {
		return fmt.Errorf("marshal candidate annotation: %w", err)
	}

	patch, err := json.Marshal(map[string]interface{}{
		"metadata": map[string]interface{}{
			"annotations": map[string]string{
				candidateNodesAnnotationKey: string(annJSON),
			},
		},
	})
	if err != nil {
		return fmt.Errorf("marshal patch: %w", err)
	}

	var lastErr error
	for attempt := 0; attempt <= maxConflictRetries; attempt++ {
		_, lastErr = sched.client.CoreV1().Pods(pod.Namespace).Patch(
			ctx, pod.Name, k8stypes.MergePatchType, patch, metav1.PatchOptions{})
		if lastErr == nil {
			break
		}
		if !apierrors.IsConflict(lastErr) {
			// Only 409 is retried here. NotFound / permanent / transient errors
			// are surfaced to the caller for differentiated handling.
			return fmt.Errorf("patch pod %s/%s: %w", pod.Namespace, pod.Name, lastErr)
		}
		if attempt == maxConflictRetries {
			break
		}
		// Short jittered backoff: 50ms, 200ms, 500ms (attempt-indexed).
		backoff := time.Duration(50*(1<<attempt)) * time.Millisecond
		select {
		case <-ctx.Done():
			return fmt.Errorf("patch pod %s/%s cancelled during retry: %w", pod.Namespace, pod.Name, ctx.Err())
		case <-time.After(backoff):
		}
	}
	if lastErr != nil {
		return fmt.Errorf("patch pod %s/%s after %d conflict retries: %w", pod.Namespace, pod.Name, maxConflictRetries, lastErr)
	}

	klog.FromContext(ctx).V(3).Info("Wrote candidate annotation",
		"pod", klog.KObj(pod), "candidates", len(candidates),
		"primary", candidates[0].Name, "scheduler", sched.parasched.name)
	return nil
}

// ---- pod filter for para-sched mode ----

// paraSchedPodFilter returns true if the pod is assigned to this scheduler
// instance by the Dispatcher (via annotation).
func (sched *Scheduler) paraSchedPodFilter(pod *v1.Pod) bool {
	if sched.parasched == nil {
		return false
	}
	return pod.Annotations[schedulerNameAnnotationKey] == sched.parasched.name
}

// computePodResourceRequest computes the total resource request for a pod
// following the Kubernetes convention: max(sum(containers), max(initContainers)).
func computePodResourceRequest(pod *v1.Pod) framework.Resource {
	var res framework.Resource
	for _, c := range pod.Spec.Containers {
		if cpu := c.Resources.Requests.Cpu(); cpu != nil {
			res.MilliCPU += cpu.MilliValue()
		}
		if mem := c.Resources.Requests.Memory(); mem != nil {
			res.Memory += mem.Value()
		}
	}
	// Init containers: take max.
	for _, c := range pod.Spec.InitContainers {
		if cpu := c.Resources.Requests.Cpu(); cpu != nil {
			if v := cpu.MilliValue(); v > res.MilliCPU {
				res.MilliCPU = v
			}
		}
		if mem := c.Resources.Requests.Memory(); mem != nil {
			if v := mem.Value(); v > res.Memory {
				res.Memory = v
			}
		}
	}
	return res
}

// ---- ParSync consumer side ----

// EnableParaSync marks the ParSync consumer mode active on the scheduler.
// Must be called after EnableParaSched(). After Phase 3, freshness bonus is
// no longer applied as a score mutation — instead it flows through the
// LatencyFirstStrategy via k8sPartitionProvider.GetPartitionStaleness, so
// this method now only flips the enableParSync flag.
func (sched *Scheduler) EnableParaSync() {
	if sched.parasched == nil {
		return
	}
	sched.parasched.enableParSync = true
}

// UpdatePartitionSyncTime records the time a partition snapshot was applied.
// Called by paraSyncConsumer after applying a snapshot. The value is consumed
// by k8sPartitionProvider.GetPartitionStaleness when LatencyFirst computes
// per-node Freshness.
func (sched *Scheduler) UpdatePartitionSyncTime(partitionID int, t time.Time) {
	if sched.parasched == nil {
		return
	}
	sched.parasched.partitionSyncMu.Lock()
	defer sched.parasched.partitionSyncMu.Unlock()
	sched.parasched.partitionSyncTimes[partitionID] = t
}

// getNodePartitionID2 retrieves the partition ID for a node from the scheduler's
// snapshot cache via its label.
func getNodePartitionID2(sched *Scheduler, nodeName string) int {
	if sched.nodeInfoSnapshot == nil {
		return -1
	}
	ni, err := sched.nodeInfoSnapshot.Get(nodeName)
	if err != nil || ni.Node() == nil {
		return -1
	}
	return getNodePartitionID(ni)
}

// IsParaSyncEnabled returns true if ParSync consumer mode is active.
func (sched *Scheduler) IsParaSyncEnabled() bool {
	return sched.parasched != nil && sched.parasched.enableParSync
}
