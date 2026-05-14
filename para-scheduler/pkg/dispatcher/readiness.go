package dispatcher

import (
	"context"
	"net/http"
	"sync/atomic"
	"time"

	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/klog/v2"

	"example.com/para-scheduler/pkg/metrics"
)

// ReadinessChecker exposes a /ready HTTP endpoint backing the Dispatcher
// Pod's K8s readinessProbe and the experiment harness's `kubectl wait`,
// gating Scheduler/Binder rollout until the Dispatcher has finished the
// work those components depend on (see docs/design-parsync-pull-fix.md §4.6.3):
//
//  1. ParSyncConfig CRD exists.
//  2. SchedulerAssignment CRDs exist for every registered scheduler.
//  3. The configured number of nodes (--expected-nodes) carry the
//     partition-id label. Without this gate, Schedulers would start with
//     unlabeled nodes (partitionID=-1) which Binder.MarkDirty silently
//     drops, leaving those nodes invisible to ParSync.
//
// Behavior is mode-aware:
//   - In event-driven mode (no PartitionAssigner) the readiness check
//     short-circuits to true once the HTTP server is up.
//   - In ParSync mode each component of the check is re-evaluated lazily
//     on every probe call; the readiness state is therefore always
//     current (no stale "ready forever" once it once flipped).
type ReadinessChecker struct {
	dispatcher    *Dispatcher
	paraClient    parasched.Interface
	expectedNodes int
	configName    string

	// readyOnce flips from 0 to 1 on the first probe that returns ready.
	// Used solely to emit the parasched_dispatcher_ready_timestamp_seconds
	// gauge exactly once (cold-start observability).
	readyOnce atomic.Bool
}

// NewReadinessChecker constructs a readiness checker. paraClient may be nil
// in event-driven mode; expectedNodes <= 0 disables the node-coverage check
// (which is correct for event-driven mode where partition labels are not
// applied at all).
func NewReadinessChecker(
	d *Dispatcher,
	paraClient parasched.Interface,
	expectedNodes int,
	configName string,
) *ReadinessChecker {
	if configName == "" {
		configName = "default"
	}
	return &ReadinessChecker{
		dispatcher:    d,
		paraClient:    paraClient,
		expectedNodes: expectedNodes,
		configName:    configName,
	}
}

// HandleReady is the http.HandlerFunc backing /ready. Returns 200 when the
// readiness conditions in the package doc are all satisfied; 503 otherwise.
//
// Note this evaluates conditions on every request (no cached "ready forever"
// flag). That keeps behavior correct if a SchedulerAssignment is later
// deleted or the operator scales node count up — the probe will start
// returning 503 again, which kubelet then reflects in the Pod's Ready
// condition.
func (r *ReadinessChecker) HandleReady(w http.ResponseWriter, _ *http.Request) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if reason, ok := r.checkReady(ctx); ok {
		if r.readyOnce.CompareAndSwap(false, true) {
			metrics.DispatcherReadyAt.Set(float64(time.Now().Unix()))
			klog.InfoS("Dispatcher transitioned to ready")
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
		return
	} else {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(reason + "\n"))
	}
}

// checkReady returns (reason-if-not-ready, ready). In event-driven mode it
// trivially returns ready; in ParSync mode it walks each precondition.
func (r *ReadinessChecker) checkReady(ctx context.Context) (string, bool) {
	// Event-driven mode: nothing to gate on. Return ready as soon as the
	// HTTP server is serving.
	if !r.dispatcher.HasPartitionAssigner() {
		return "", true
	}

	if r.paraClient == nil {
		return "paraClient nil (ParSync mode requires CRD client)", false
	}

	// 1. ParSyncConfig present.
	if _, err := r.paraClient.SchedulingV1().ParSyncConfigs().Get(ctx, r.configName, metav1.GetOptions{}); err != nil {
		if errors.IsNotFound(err) {
			return "ParSyncConfig CRD not yet created", false
		}
		return "ParSyncConfig get error: " + err.Error(), false
	}

	// 2. SchedulerAssignment for each registered scheduler.
	r.dispatcher.schedulerMu.RLock()
	expectedAssignments := make([]string, 0, len(r.dispatcher.schedulers))
	for name := range r.dispatcher.schedulers {
		expectedAssignments = append(expectedAssignments, name)
	}
	r.dispatcher.schedulerMu.RUnlock()
	for _, name := range expectedAssignments {
		if _, err := r.paraClient.SchedulingV1().SchedulerAssignments().Get(ctx, name, metav1.GetOptions{}); err != nil {
			if errors.IsNotFound(err) {
				return "SchedulerAssignment missing for " + name, false
			}
			return "SchedulerAssignment get error: " + err.Error(), false
		}
	}

	// 3. Expected node count carries the partition-id label.
	if r.expectedNodes > 0 {
		if r.dispatcher.nodeLister == nil {
			return "nodeLister not yet wired", false
		}
		// Use a label-existence selector so we never list every node — the
		// API server filters in-place.
		req, _ := labels.NewRequirement(PartitionIDLabel, "exists", nil)
		labeled, err := r.dispatcher.nodeLister.List(labels.NewSelector().Add(*req))
		if err != nil {
			return "node list error: " + err.Error(), false
		}
		if len(labeled) < r.expectedNodes {
			return "labeled nodes below threshold", false
		}
	}

	return "", true
}
