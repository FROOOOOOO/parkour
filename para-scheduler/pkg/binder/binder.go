package binder

import (
	"context"
	"errors"
	"fmt"
	"time"

	v1 "k8s.io/api/core/v1"
	k8stypes "k8s.io/apimachinery/pkg/types"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clientset "k8s.io/client-go/kubernetes"
	listersv1 "k8s.io/client-go/listers/core/v1"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
	"k8s.io/klog/v2"

	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"example.com/scheduler-lib/types"

	"example.com/para-scheduler/pkg/annotation"
	bindercache "example.com/para-scheduler/pkg/cache"
	"example.com/para-scheduler/pkg/metrics"
)

var (
	// ErrAllCandidatesFailed is returned when all candidate nodes fail conflict check or bind.
	ErrAllCandidatesFailed = errors.New("all candidate nodes failed")

	// ErrNoCandidates is returned when the pod has no candidate nodes annotation.
	ErrNoCandidates = errors.New("no candidate nodes in annotation")
)

// Binder watches for pods with candidate-node annotations and binds them,
// trying candidates in rank order with fallback.
type Binder struct {
	client    clientset.Interface
	crdClient parasched.Interface
	cache     *bindercache.BinderCache
	queue     workqueue.RateLimitingInterface

	// podLister is used by processQueueItem to look up pod objects by key.
	// Injected after Informer factory is created (see SetPodLister).
	podLister listersv1.PodLister

	// feasibility is the ordered rule chain evaluated against each candidate
	// node before its bind is attempted. NewBinder installs
	// DefaultFeasibilityRules; SetFeasibilityRules replaces it.
	feasibility FeasibilityRules

	// [ParSync] snapshot publisher (nil when not in ParSync mode)
	snapshotPublisher *SnapshotPublisher

	// Binding result reporter (nil if CRD client not configured)
	reporter *BindingReporter
}

// NewBinder creates a Binder with the given clients and cache.
func NewBinder(
	client clientset.Interface,
	crdClient parasched.Interface,
	binderCache *bindercache.BinderCache,
) *Binder {
	return &Binder{
		client:      client,
		crdClient:   crdClient,
		cache:       binderCache,
		feasibility: DefaultFeasibilityRules(),
		queue: workqueue.NewRateLimitingQueue(
			workqueue.NewItemExponentialFailureRateLimiter(100*time.Millisecond, 30*time.Second)),
	}
}

// SetFeasibilityRules replaces the commit-time feasibility chain evaluated
// against each candidate node. Must be called before Run.
//
// Returns an error when rules is empty: an empty chain rejects every candidate
// (see FeasibilityRules.Check), which is never a useful configuration. To admit
// every candidate, supply a rule that does so explicitly.
func (b *Binder) SetFeasibilityRules(rules FeasibilityRules) error {
	if len(rules) == 0 {
		return fmt.Errorf("feasibility rule chain must not be empty")
	}
	b.feasibility = rules
	return nil
}

// SetPodLister injects the Pod lister used to look up pods by key.
// Must be called before Run, typically after the Informer factory is created.
func (b *Binder) SetPodLister(lister listersv1.PodLister) {
	b.podLister = lister
}

// SetSnapshotPublisher attaches a snapshot publisher for ParSync mode.
func (b *Binder) SetSnapshotPublisher(sp *SnapshotPublisher) {
	b.snapshotPublisher = sp
}

// SetReporter attaches a binding result reporter.
func (b *Binder) SetReporter(r *BindingReporter) {
	b.reporter = r
}

// Run starts the binder's work loop with the given number of workers.
// Blocks until ctx is cancelled.
func (b *Binder) Run(ctx context.Context, workers int) {
	defer b.queue.ShutDown()

	klog.InfoS("Starting Binder", "workers", workers)
	for i := 0; i < workers; i++ {
		go b.worker(ctx)
	}
	<-ctx.Done()
	klog.InfoS("Binder stopped")
}

// Enqueue adds a pod key to the work queue. Called by event handlers.
func (b *Binder) Enqueue(pod *v1.Pod) {
	key, err := cache.MetaNamespaceKeyFunc(pod)
	if err != nil {
		klog.ErrorS(err, "Failed to compute pod key")
		return
	}
	b.queue.Add(key)
}

// worker processes items from the queue until ctx is done.
func (b *Binder) worker(ctx context.Context) {
	for {
		item, shutdown := b.queue.Get()
		if shutdown {
			return
		}
		key := item.(string)
		err := b.processQueueItem(ctx, key)
		if err != nil {
			klog.ErrorS(err, "Failed to process pod", "key", key)
			b.queue.AddRateLimited(key)
		} else {
			b.queue.Forget(key)
		}
		b.queue.Done(item)
	}
}

// processQueueItem looks up the pod via the Pod Lister and calls ProcessPod.
func (b *Binder) processQueueItem(ctx context.Context, key string) error {
	ns, name, err := cache.SplitMetaNamespaceKey(key)
	if err != nil {
		return fmt.Errorf("invalid pod key %q: %w", key, err)
	}

	pod, err := b.podLister.Pods(ns).Get(name)
	if err != nil {
		// Pod may have been deleted — skip silently.
		klog.V(4).InfoS("Pod not found in lister, skipping", "key", key)
		return nil
	}

	// Re-check whether the pod still needs binding (it may have been
	// bound or deleted between enqueue and dequeue).
	if !NeedsBinding(pod) {
		klog.V(5).InfoS("Pod no longer needs binding, skipping", "key", key)
		return nil
	}

	return b.ProcessPod(ctx, pod)
}

// ProcessPod handles a single pod's binding with candidate fallback.
// This is the core Binder logic:
//  1. Decode candidate list from annotation
//  2. For each candidate in rank order:
//     a. Check feasibility (default rule: resource fit)
//     b. Assume pod on node (optimistic)
//     c. Call bind API
//     d. On failure: forget pod, try next candidate
//  3. On success: report result, mark snapshot dirty
//  4. If all fail: report failure

// recordCandidateFailure reports a per-candidate (per-node) bind failure to the
// AdoptionStats reporter so that scheduler-lib's per-node SlidingWindow can
// compute a real per-node conflict rate (used by the Penalty mechanism).
//
// Without this, only ACF outcomes reached the reporter (with empty NodeName),
// making nodeStats accumulate wins-only, collapsing GetNodeConflictRate to 0
// for every node and degenerating the Penalty formula to an affine no-op.
// See paper §penalty and scheduler-lib/multicandidate/strategy.go for the
// combinedScore formula that depends on per-node conflict rate.
func (b *Binder) recordCandidateFailure(podKey string, c annotation.CandidateEntry, attempt int, reason string) {
	if b.reporter == nil {
		return
	}
	b.reporter.Record(types.BindingResult{
		PodKey:       podKey,
		NodeName:     c.NodeName,
		Rank:         c.Rank,
		Success:      false,
		Timestamp:    time.Now(),
		PartitionID:  c.PartitionID,
		AttemptCount: attempt,
		ErrorMsg:     reason,
	})
}

func (b *Binder) ProcessPod(ctx context.Context, pod *v1.Pod) error {
	// 1. Decode candidate annotation.
	raw, ok := pod.Annotations[annotation.CandidateNodesAnnotationKey]
	if !ok {
		return ErrNoCandidates
	}
	ann, err := annotation.Decode(raw)
	if err != nil {
		return fmt.Errorf("decode candidate annotation: %w", err)
	}
	if len(ann.Candidates) == 0 {
		return ErrNoCandidates
	}

	podKey := bindercache.PodKeyFunc(pod)

	// Soft fast-path: skip early if obviously already assumed. This is only a
	// latency/noise optimization — the real concurrency guard is TryAssumePod
	// inside the candidate loop, which performs the check-and-assume under a
	// single mutex acquisition (closing the TOCTOU window that would
	// otherwise let two workers process the same pod concurrently).
	if b.cache.IsAssumedPodByKey(podKey) {
		klog.V(4).InfoS("Pod already assumed (soft fast-path), skipping", "pod", podKey)
		return nil
	}

	klog.V(4).InfoS("Processing pod binding", "pod", podKey,
		"candidates", len(ann.Candidates), "scheduler", ann.Scheduler)

	startTime := time.Now()

	// 2. Try each candidate in rank order.
	for i, candidate := range ann.Candidates {
		// a. Get node info from cache.
		nodeInfo := b.cache.GetNodeInfo(candidate.NodeName)
		if nodeInfo == nil {
			klog.V(4).InfoS("Candidate node not found in cache",
				"pod", podKey, "node", candidate.NodeName, "rank", i)
			metrics.RecordConflict(candidate.NodeName, "node_not_found")
			b.recordCandidateFailure(podKey, candidate, i+1, "node_not_found")
			continue
		}

		// b. Check feasibility against the Binder's running account of
		//    admitted requests.
		fits, reason := b.feasibility.Check(pod, nodeInfo)
		if !fits {
			klog.V(4).InfoS("Candidate node conflict",
				"pod", podKey, "node", candidate.NodeName, "rank", i, "reason", reason)
			metrics.RecordConflict(candidate.NodeName, reason)
			b.recordCandidateFailure(podKey, candidate, i+1, reason)
			continue
		}

		// c. Atomically check+assume under cache.mu. If another worker has
		//    already taken this pod (assumed=false, err=nil), abandon the
		//    cycle WITHOUT clearing annotations — the in-flight worker may
		//    still succeed and clearing would trigger a spurious re-dispatch.
		assumed, err := b.cache.TryAssumePod(pod, candidate.NodeName)
		if err != nil {
			klog.ErrorS(err, "Failed to assume pod",
				"pod", podKey, "node", candidate.NodeName)
			continue
		}
		if !assumed {
			klog.V(3).InfoS("Pod already assumed by concurrent worker, abandoning cycle",
				"pod", podKey, "node", candidate.NodeName, "rank", i)
			return nil
		}

		// d. Call bind API.
		bindErr := bind(ctx, b.client, pod, candidate.NodeName)
		if bindErr != nil {
			// Bind failed — forget the assume and try next candidate.
			// We just TryAssumePod'd successfully above, so ForgetPod should
			// normally succeed. A failure here means something concurrent
			// removed the pod from the cache (TTL expiry / Informer delete /
			// another worker) — surface this as an error log so resource
			// accounting drift is observable instead of silently leaking the
			// assumed resources on the candidate node.
			if forgetErr := b.cache.ForgetPod(pod); forgetErr != nil {
				klog.ErrorS(forgetErr, "ForgetPod failed after bind failure; node resources may drift",
					"pod", podKey, "node", candidate.NodeName)
			}
			klog.V(3).InfoS("Bind API call failed",
				"pod", podKey, "node", candidate.NodeName, "rank", i, "err", bindErr)
			metrics.RecordConflict(candidate.NodeName, bindErr.Error())
			b.recordCandidateFailure(podKey, candidate, i+1, bindErr.Error())
			continue
		}

		// Success!
		duration := time.Since(startTime)
		klog.V(3).InfoS("Pod bound successfully",
			"pod", podKey, "node", candidate.NodeName, "rank", i,
			"attempts", i+1, "duration", duration)

		metrics.RecordBindSuccess(candidate.NodeName, i, duration)

		// Report to AdoptionStats CRD.
		if b.reporter != nil {
			b.reporter.Record(types.BindingResult{
				PodKey:       podKey,
				NodeName:     candidate.NodeName,
				Rank:         candidate.Rank,
				Success:      true,
				Timestamp:    time.Now(),
				PartitionID:  candidate.PartitionID,
				AttemptCount: i + 1,
			})
		}

		// [ParSync] Mark partition dirty for batched snapshot publish.
		// Prefer the Binder cache's partition ID (authoritative because the
		// Dispatcher writes it via node label), but only when it is valid
		// (>= 0). If the node has not yet been labelled (PartitionID=-1,
		// the "unassigned" sentinel), fall back to the annotation value the
		// Scheduler computed at decision time — otherwise MarkDirty(-1) is
		// silently dropped and the snapshot for this partition is never
		// published.
		if b.snapshotPublisher != nil {
			pid := candidate.PartitionID
			if ni := b.cache.GetNodeInfo(candidate.NodeName); ni != nil && ni.PartitionID >= 0 {
				pid = ni.PartitionID
			}
			b.snapshotPublisher.MarkDirty(pid)
		}

		return nil
	}

	// 3. All candidates failed — clear annotations so the pod goes back
	//    through Dispatcher → Scheduler for a fresh scheduling cycle.
	klog.V(2).InfoS("All candidates failed for pod, clearing annotations for reschedule",
		"pod", podKey, "totalCandidates", len(ann.Candidates))

	metrics.RecordAllCandidatesFailed(podKey)

	if b.reporter != nil {
		b.reporter.Record(types.BindingResult{
			PodKey:       podKey,
			Success:      false,
			Timestamp:    time.Now(),
			AttemptCount: len(ann.Candidates),
			ErrorMsg:     "all candidates failed",
		})
	}

	// Remove both scheduler-name and candidate-nodes annotations.
	// This makes the pod eligible for re-dispatch by the Dispatcher.
	patch := []byte(`{"metadata":{"annotations":{` +
		`"` + annotation.SchedulerNameAnnotationKey + `":null,` +
		`"` + annotation.CandidateNodesAnnotationKey + `":null}}}`)
	_, patchErr := b.client.CoreV1().Pods(pod.Namespace).Patch(
		ctx, pod.Name, k8stypes.MergePatchType, patch, metav1.PatchOptions{})
	if patchErr != nil {
		klog.ErrorS(patchErr, "Failed to clear annotations for reschedule", "pod", podKey)
		return ErrAllCandidatesFailed // still return error so queue retries the clear
	}

	// Return nil — the pod will be re-enqueued by Dispatcher after it detects
	// the cleared annotations. Don't re-add to Binder queue.
	return nil
}
