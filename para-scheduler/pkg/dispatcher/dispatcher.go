// Package dispatcher implements a simplified Dispatcher that watches pending
// pods and distributes them to Scheduler instances via pod annotations.
// In ParSync mode it additionally manages partition assignments and CRD creation.
package dispatcher

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	k8stypes "k8s.io/apimachinery/pkg/types"
	clientset "k8s.io/client-go/kubernetes"
	listersv1 "k8s.io/client-go/listers/core/v1"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
	"k8s.io/klog/v2"

	"example.com/para-scheduler/pkg/annotation"
	"example.com/para-scheduler/pkg/metrics"
)

// SchedulerInfo tracks the state of a single scheduler instance.
type SchedulerInfo struct {
	Name     string
	PodCount int64 // number of pods currently assigned (for load balancing)
}

// Dispatcher watches pending pods and assigns them to scheduler instances
// using least-loaded selection, then patches the pod annotation.
type Dispatcher struct {
	client     clientset.Interface
	podLister  listersv1.PodLister
	podQueue   workqueue.RateLimitingInterface

	// [ParSync] node workqueue for partition-id label patching with retries.
	// OnNodeAdd enqueues the node name; a dedicated worker re-looks-up the
	// node from nodeLister, re-computes partition assignment (idempotent via
	// PartitionManager), and patches the label — failures go back to the
	// queue under exponential backoff. Without this, a single transient patch
	// failure leaves the node unlabelled, which causes every Scheduler to
	// skip freshness bonus for that node (pid<0) and silently degrades
	// ParSync into non-ParSync for the affected node.
	nodeLister listersv1.NodeLister
	nodeQueue  workqueue.RateLimitingInterface

	schedulerMu sync.RWMutex
	schedulers   map[string]*SchedulerInfo // schedulerName -> info

	// [ParSync] partition assigner (nil when not in ParSync mode)
	partitionAssigner *PartitionAssigner

	// [ParSync] coordinator replaces the old static PartitionAssigner for dynamic mode
	coordinator *ParSyncCoordinator // nil when not in ParSync mode
}

// NewDispatcher creates a Dispatcher with the given scheduler names.
func NewDispatcher(client clientset.Interface, schedulerNames []string) *Dispatcher {
	schedulers := make(map[string]*SchedulerInfo, len(schedulerNames))
	for _, name := range schedulerNames {
		schedulers[name] = &SchedulerInfo{Name: name}
	}
	return &Dispatcher{
		client:     client,
		schedulers: schedulers,
		podQueue: workqueue.NewRateLimitingQueue(
			workqueue.NewItemExponentialFailureRateLimiter(100*time.Millisecond, 30*time.Second)),
		nodeQueue: workqueue.NewRateLimitingQueue(
			workqueue.NewItemExponentialFailureRateLimiter(100*time.Millisecond, 30*time.Second)),
	}
}

// SetPodLister injects the Pod lister for looking up pods by key.
func (d *Dispatcher) SetPodLister(lister listersv1.PodLister) {
	d.podLister = lister
}

// SetNodeLister injects the Node lister used by the partition-label retry loop.
func (d *Dispatcher) SetNodeLister(lister listersv1.NodeLister) {
	d.nodeLister = lister
}

// SetPartitionAssigner attaches a PartitionAssigner for ParSync mode.
func (d *Dispatcher) SetPartitionAssigner(pa *PartitionAssigner) {
	d.partitionAssigner = pa
}

// HasPartitionAssigner returns true if a PartitionAssigner is configured.
func (d *Dispatcher) HasPartitionAssigner() bool {
	return d.partitionAssigner != nil
}

// SetCoordinator attaches a ParSyncCoordinator for dynamic ParSync mode.
func (d *Dispatcher) SetCoordinator(c *ParSyncCoordinator) {
	d.coordinator = c
}

// GetCoordinator returns the coordinator (may be nil).
func (d *Dispatcher) GetCoordinator() *ParSyncCoordinator {
	return d.coordinator
}

// AddScheduler adds a scheduler to the dispatcher's scheduler map.
// In ParSync mode, also notifies the coordinator.
func (d *Dispatcher) AddScheduler(ctx context.Context, name string) {
	d.schedulerMu.Lock()
	if _, ok := d.schedulers[name]; !ok {
		d.schedulers[name] = &SchedulerInfo{Name: name}
	}
	d.schedulerMu.Unlock()

	if d.coordinator != nil {
		if err := d.coordinator.RegisterScheduler(ctx, name); err != nil {
			klog.ErrorS(err, "Failed to register scheduler with coordinator", "name", name)
		}
	}
}

// RemoveScheduler removes a scheduler from the dispatcher's scheduler map.
// In ParSync mode, also notifies the coordinator.
func (d *Dispatcher) RemoveScheduler(ctx context.Context, name string) {
	d.schedulerMu.Lock()
	delete(d.schedulers, name)
	d.schedulerMu.Unlock()

	if d.coordinator != nil {
		if err := d.coordinator.DeregisterScheduler(ctx, name); err != nil {
			klog.ErrorS(err, "Failed to deregister scheduler with coordinator", "name", name)
		}
	}
}

// RecoverPodCounts rebuilds each scheduler's in-flight PodCount by scanning
// all pods from the lister. This should be called once after Informer sync
// completes and before starting workers, so that a Dispatcher restart does not
// lose track of already-dispatched pods.
func (d *Dispatcher) RecoverPodCounts() {
	if d.podLister == nil {
		return
	}
	pods, err := d.podLister.List(labels.Everything())
	if err != nil {
		klog.ErrorS(err, "Failed to list pods for PodCount recovery")
		return
	}

	d.schedulerMu.Lock()
	defer d.schedulerMu.Unlock()

	// Reset all counts first.
	for _, info := range d.schedulers {
		info.PodCount = 0
	}

	for _, pod := range pods {
		// Count pods that are dispatched (have scheduler annotation) but not yet
		// bound (NodeName still empty) and not in a terminal phase.
		if pod.Spec.NodeName != "" {
			continue
		}
		if pod.Status.Phase == v1.PodSucceeded || pod.Status.Phase == v1.PodFailed {
			continue
		}
		schedulerName, ok := pod.Annotations[annotation.SchedulerNameAnnotationKey]
		if !ok {
			continue
		}
		if info, ok := d.schedulers[schedulerName]; ok {
			info.PodCount++
		}
	}

	for name, info := range d.schedulers {
		klog.V(3).InfoS("Recovered PodCount", "scheduler", name, "count", info.PodCount)
	}
}

// Run starts the dispatcher work loop. Blocks until ctx is cancelled.
func (d *Dispatcher) Run(ctx context.Context, workers int) {
	defer d.podQueue.ShutDown()
	defer d.nodeQueue.ShutDown()

	d.RecoverPodCounts()
	klog.InfoS("Starting Dispatcher", "workers", workers, "schedulers", len(d.schedulers))
	for i := 0; i < workers; i++ {
		go d.worker(ctx)
	}
	// [ParSync] Node workers handle partition-label patches with retry.
	// Workqueue's Get/Done/Forget already dedups in-flight keys (same node
	// cannot be processed by two workers simultaneously), so we parallelize
	// across `workers` to keep readiness time bounded at large node counts:
	// at 20000 nodes a single worker takes ~48s of single-flight patches,
	// which approaches the 120s readiness probe budget.
	if d.HasPartitionAssigner() {
		for i := 0; i < workers; i++ {
			go d.nodeWorker(ctx)
		}
	}
	<-ctx.Done()
	klog.InfoS("Dispatcher stopped")
}

// Enqueue adds a pod key to the work queue.
func (d *Dispatcher) Enqueue(pod *v1.Pod) {
	key, err := cache.MetaNamespaceKeyFunc(pod)
	if err != nil {
		klog.ErrorS(err, "Failed to compute pod key")
		return
	}
	d.podQueue.Add(key)
}

// EnqueueNode adds a node name to the partition-label workqueue. Called from
// OnNodeAdd (ParSync mode) and from the worker itself on transient patch
// failure via AddRateLimited.
func (d *Dispatcher) EnqueueNode(nodeName string) {
	if nodeName == "" {
		return
	}
	d.nodeQueue.Add(nodeName)
}

// nodeWorker drains nodeQueue and (re)patches partition-id labels with
// exponential backoff on transient failures.
func (d *Dispatcher) nodeWorker(ctx context.Context) {
	for {
		item, shutdown := d.nodeQueue.Get()
		if shutdown {
			return
		}
		nodeName := item.(string)
		if err := d.processNodeQueueItem(ctx, nodeName); err != nil {
			klog.ErrorS(err, "Failed to patch partition label on node, retrying", "node", nodeName)
			d.nodeQueue.AddRateLimited(nodeName)
		} else {
			d.nodeQueue.Forget(nodeName)
		}
		d.nodeQueue.Done(item)
	}
}

// processNodeQueueItem handles one node's partition label patch. Idempotent:
//   - if the label is already present, return nil (Forget);
//   - if the node is gone from the lister (NotFound), return nil (Forget);
//   - otherwise compute pid (idempotent via PartitionManager) and patch;
//   - any patch error is returned to trigger rate-limited retry.
func (d *Dispatcher) processNodeQueueItem(ctx context.Context, nodeName string) error {
	if d.partitionAssigner == nil || d.nodeLister == nil {
		return nil
	}
	node, err := d.nodeLister.Get(nodeName)
	if err != nil {
		klog.V(4).InfoS("Node not found in lister, skipping partition label patch", "node", nodeName)
		return nil
	}
	if _, ok := node.Labels[PartitionIDLabel]; ok {
		return nil
	}

	pid := d.partitionAssigner.RegisterNode(node.Name)

	patch, err := json.Marshal(map[string]interface{}{
		"metadata": map[string]interface{}{
			"labels": map[string]string{
				PartitionIDLabel: fmt.Sprintf("%d", pid),
			},
		},
	})
	if err != nil {
		return fmt.Errorf("marshal partition label patch: %w", err)
	}
	if _, err := d.client.CoreV1().Nodes().Patch(
		ctx, node.Name, k8stypes.MergePatchType, patch, metav1.PatchOptions{}); err != nil {
		return fmt.Errorf("patch partition label: %w", err)
	}
	klog.V(4).InfoS("Assigned node to partition", "node", node.Name, "partition", pid)
	return nil
}

// worker processes items from the queue.
func (d *Dispatcher) worker(ctx context.Context) {
	for {
		item, shutdown := d.podQueue.Get()
		if shutdown {
			return
		}
		metrics.SetDispatcherQueueDepth(d.podQueue.Len())
		key := item.(string)
		if err := d.processQueueItem(ctx, key); err != nil {
			klog.ErrorS(err, "Failed to dispatch pod", "key", key)
			d.podQueue.AddRateLimited(key)
		} else {
			d.podQueue.Forget(key)
		}
		d.podQueue.Done(item)
	}
}

// processQueueItem looks up the pod and dispatches it.
func (d *Dispatcher) processQueueItem(ctx context.Context, key string) error {
	ns, name, err := cache.SplitMetaNamespaceKey(key)
	if err != nil {
		return fmt.Errorf("invalid pod key %q: %w", key, err)
	}

	pod, err := d.podLister.Pods(ns).Get(name)
	if err != nil {
		klog.V(4).InfoS("Pod not found, skipping", "key", key)
		return nil
	}

	if !NeedsDispatch(pod) {
		return nil
	}

	return d.DispatchPod(ctx, pod)
}

// DispatchPod selects a scheduler and patches the pod annotation.
func (d *Dispatcher) DispatchPod(ctx context.Context, pod *v1.Pod) error {
	start := time.Now()

	schedulerName := d.selectScheduler()
	if schedulerName == "" {
		metrics.RecordDispatchError("")
		return fmt.Errorf("no schedulers available")
	}

	// Patch pod annotation with assigned scheduler name.
	patch := map[string]interface{}{
		"metadata": map[string]interface{}{
			"annotations": map[string]string{
				annotation.SchedulerNameAnnotationKey: schedulerName,
			},
		},
	}
	patchData, err := json.Marshal(patch)
	if err != nil {
		metrics.RecordDispatchError(schedulerName)
		return fmt.Errorf("marshal patch: %w", err)
	}

	_, err = d.client.CoreV1().Pods(pod.Namespace).Patch(
		ctx, pod.Name, k8stypes.MergePatchType, patchData, metav1.PatchOptions{})
	if err != nil {
		metrics.RecordDispatchError(schedulerName)
		return fmt.Errorf("patch pod %s/%s: %w", pod.Namespace, pod.Name, err)
	}

	// Re-validate scheduler existence under the write lock before incrementing.
	// Race: selectScheduler reads schedulers under RLock then releases; between
	// that release and this Lock, RemoveScheduler (driven by scheduler pod
	// Informer delete) can remove the chosen name. If we just silently skip
	// the increment, the pod keeps an annotation pointing at a dead scheduler
	// and becomes an orphan — NeedsDispatch returns false on re-entry, so it
	// never gets re-dispatched.
	//
	// Under the Lock, atomically check-and-increment; if the scheduler is
	// gone, best-effort rollback the annotation (so the pod re-enters
	// NeedsDispatch on the next informer event) and return an error so the
	// worker requeues.
	d.schedulerMu.Lock()
	info, ok := d.schedulers[schedulerName]
	if ok {
		info.PodCount++
	}
	d.schedulerMu.Unlock()

	if !ok {
		// Best-effort rollback: clear the annotation so NeedsDispatch picks
		// the pod up again. If the rollback patch itself fails, the pod is
		// briefly orphan until the next reconcile; we surface via metric.
		rollback := []byte(`{"metadata":{"annotations":{"` + annotation.SchedulerNameAnnotationKey + `":null}}}`)
		if _, rbErr := d.client.CoreV1().Pods(pod.Namespace).Patch(
			ctx, pod.Name, k8stypes.MergePatchType, rollback, metav1.PatchOptions{}); rbErr != nil {
			klog.ErrorS(rbErr, "Failed to rollback annotation after scheduler vanished",
				"pod", pod.Namespace+"/"+pod.Name, "scheduler", schedulerName)
		}
		metrics.RecordDispatchError(schedulerName)
		return fmt.Errorf("scheduler %q vanished between select and increment; annotation rolled back", schedulerName)
	}

	metrics.RecordDispatchSuccess(schedulerName, time.Since(start))
	klog.V(4).InfoS("Pod dispatched",
		"pod", pod.Namespace+"/"+pod.Name, "scheduler", schedulerName)
	return nil
}

// selectScheduler picks the scheduler with the lowest PodCount (least-loaded).
func (d *Dispatcher) selectScheduler() string {
	d.schedulerMu.RLock()
	defer d.schedulerMu.RUnlock()

	var best string
	var bestCount int64 = -1

	for name, info := range d.schedulers {
		if bestCount < 0 || info.PodCount < bestCount {
			best = name
			bestCount = info.PodCount
		}
	}
	return best
}

// DecrementPodCount decreases the pod count for a scheduler.
// Called when a pod finishes (bound or failed).
func (d *Dispatcher) DecrementPodCount(schedulerName string) {
	d.schedulerMu.Lock()
	defer d.schedulerMu.Unlock()
	if info, ok := d.schedulers[schedulerName]; ok && info.PodCount > 0 {
		info.PodCount--
		metrics.RecordSchedulerDecrement(schedulerName)
	}
}

// GetSchedulerInfo returns a copy of the scheduler info (for testing).
func (d *Dispatcher) GetSchedulerInfo(name string) *SchedulerInfo {
	d.schedulerMu.RLock()
	defer d.schedulerMu.RUnlock()
	if info, ok := d.schedulers[name]; ok {
		cp := *info
		return &cp
	}
	return nil
}

// NeedsDispatch returns true if a pod needs to be dispatched:
//   - NodeName is empty (not yet bound)
//   - Not being deleted (no DeletionTimestamp)
//   - No scheduler annotation (not yet dispatched)
//   - Not in terminal phase
func NeedsDispatch(pod *v1.Pod) bool {
	if pod.Spec.NodeName != "" {
		return false
	}
	// Pods under deletion must not be dispatched: the API server will
	// garbage-collect them regardless, and dispatching allocates scheduler
	// slot quota + triggers Scheduler binding attempts that race the delete.
	if pod.ObjectMeta.DeletionTimestamp != nil {
		return false
	}
	if pod.Status.Phase == v1.PodSucceeded || pod.Status.Phase == v1.PodFailed {
		return false
	}
	if _, hasScheduler := pod.Annotations[annotation.SchedulerNameAnnotationKey]; hasScheduler {
		return false
	}
	// Already has candidates means scheduler already processed it.
	if _, hasCandidates := pod.Annotations[annotation.CandidateNodesAnnotationKey]; hasCandidates {
		return false
	}
	return true
}
