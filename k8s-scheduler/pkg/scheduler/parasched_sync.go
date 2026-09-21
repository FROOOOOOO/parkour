/*
ParSync consumer side for the scheduler: watches partition snapshot ConfigMaps
published by the Binder, and applies them to the local scheduler cache using
a rotation window strategy.

Also implements the AdoptionStats CRD watcher that feeds conflict rates
into the scheduler's penalty mechanism.
*/
package scheduler

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"sync"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
	parasched "example.com/para-sched-api/generated/clientset/versioned"
	parainformers "example.com/para-sched-api/generated/informers/externalversions"
	libparsync "example.com/scheduler-lib/parsync"
	libstats "example.com/scheduler-lib/stats"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
	"k8s.io/klog/v2"
	"k8s.io/kubernetes/pkg/scheduler/framework"
	schedmetrics "k8s.io/kubernetes/pkg/scheduler/metrics"
)

// Snapshot wire format (labels, JSON types, marshal helpers) is defined in
// scheduler-lib so the Binder publisher and the Scheduler consumer cannot
// drift. We use libparsync.Snapshot / SnapshotNodeEntry / SnapshotResource
// directly throughout this file.

// ---- ParSync consumer ----

// chunkSet accumulates chunks of a single (partitionID, generation) pair
// until all chunks are present. Once complete, it can be merged into a
// single libparsync.Snapshot for apply.
type chunkSet struct {
	generation int64
	chunkCount int
	chunks     map[int]*libparsync.Snapshot // chunkIndex -> chunk content
}

// paraSyncConsumer watches partition snapshot ConfigMaps and applies them
// to the scheduler's node info snapshot using a rotation window.
type paraSyncConsumer struct {
	kubeClient kubernetes.Interface
	sched      *Scheduler
	namespace  string // ConfigMap namespace, default "para-system"

	// Partition configuration (from SchedulerAssignment CRD).
	numPartitions  int
	syncPeriod     time.Duration
	partitionOrder []int // order in which this scheduler syncs partitions

	// Chunk buffers per partition: accumulate chunks of the newest generation
	// until the full chunk set arrives, then promote to pendingSnapshots.
	pendingMu        sync.Mutex
	chunkBuffers     map[int]*chunkSet          // partitionID -> in-progress chunkSet
	pendingSnapshots map[int]*libparsync.Snapshot // partitionID -> merged snapshot ready to apply

	// Generation tracking to reject stale snapshots.
	appliedGenerations map[int]int64 // partitionID -> last applied generation

	// startedAt records when this consumer was created. Snapshots with a
	// Timestamp before startedAt are rejected — they carry stale data from
	// a previous experiment/trial whose Binder has since been restarted.
	startedAt time.Time

	// readyCh is closed once every partition has received at least one valid
	// snapshot (or the wait timeout expires). The rotation loop blocks on
	// this before applying snapshots so that the scheduler doesn't operate
	// on a mix of Informer-only and partially-applied snapshot state.
	readyCh chan struct{}
}

// newParaSyncConsumer creates a new consumer.
func newParaSyncConsumer(
	kubeClient kubernetes.Interface,
	sched *Scheduler,
	namespace string,
	numPartitions int,
	syncPeriod time.Duration,
	partitionOrder []int,
) *paraSyncConsumer {
	if namespace == "" {
		namespace = "para-system"
	}
	return &paraSyncConsumer{
		kubeClient:         kubeClient,
		sched:              sched,
		namespace:          namespace,
		numPartitions:      numPartitions,
		syncPeriod:         syncPeriod,
		partitionOrder:     partitionOrder,
		chunkBuffers:       make(map[int]*chunkSet),
		pendingSnapshots:   make(map[int]*libparsync.Snapshot),
		appliedGenerations: make(map[int]int64),
		startedAt:          time.Now(),
		readyCh:            make(chan struct{}),
	}
}

// Run starts the ParSync consumer: watches ConfigMaps and applies snapshots
// on a rotation schedule. Blocks until ctx is cancelled.
func (c *paraSyncConsumer) Run(ctx context.Context) {
	// One unified watcher across all partitions, selected by label. Cheaper
	// than numPartitions connections, and naturally handles P being dynamic
	// (e.g., driven by ParSyncConfig CRD).
	go c.watchAllChunks(ctx)

	// Wait until every partition has received at least one complete
	// (all chunks present) snapshot whose Timestamp >= startedAt, so the
	// scheduler doesn't operate on partial or stale cross-experiment data.
	c.waitForInitialSnapshots(ctx)

	// Start rotation window loop.
	c.rotationLoop(ctx)
}

// watchAllChunks does a single List-then-Watch on all snapshot chunk
// ConfigMaps (selected by label). Each chunk event is routed into the
// appropriate partition's chunkBuffer via handleChunkCM; when a chunk set
// becomes complete, the merged snapshot moves into pendingSnapshots.
func (c *paraSyncConsumer) watchAllChunks(ctx context.Context) {
	labelSelector := fmt.Sprintf("%s=%s", libparsync.SnapshotLabelApp, libparsync.SnapshotLabelAppValue)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// Bootstrap: List current chunks so we don't miss a complete
		// generation that was already published before our watcher ran.
		list, err := c.kubeClient.CoreV1().ConfigMaps(c.namespace).List(ctx, metav1.ListOptions{
			LabelSelector: labelSelector,
		})
		if err != nil {
			klog.ErrorS(err, "Failed to list snapshot chunk ConfigMaps")
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}
		for i := range list.Items {
			c.handleChunkCM(&list.Items[i])
		}

		watcher, err := c.kubeClient.CoreV1().ConfigMaps(c.namespace).Watch(ctx, metav1.ListOptions{
			LabelSelector:   labelSelector,
			ResourceVersion: list.ResourceVersion,
		})
		if err != nil {
			klog.ErrorS(err, "Failed to watch snapshot chunk ConfigMaps")
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}

		klog.InfoS("Snapshot chunk watcher established",
			"namespace", c.namespace, "selector", labelSelector,
			"bootstrapCount", len(list.Items))

		for event := range watcher.ResultChan() {
			cm, ok := event.Object.(*v1.ConfigMap)
			if !ok {
				continue
			}
			// Deletions are expected during stale-tail cleanup after the
			// chunk count shrinks (Binder SnapshotPublisher.cleanupStaleChunks).
			// Those chunks belong to a generation already superseded by the
			// newer publish, so we don't need to mutate chunkBuffers — the
			// newer generation already reset the buffer.
			if event.Type == watch.Deleted {
				continue
			}
			c.handleChunkCM(cm)
		}
		klog.InfoS("Snapshot chunk watcher closed, restarting")
	}
}

// handleChunkCM parses a single chunk ConfigMap and feeds it into the
// appropriate partition's chunkBuffer. If the chunk completes the set for
// its generation, the merged snapshot is promoted to pendingSnapshots.
func (c *paraSyncConsumer) handleChunkCM(cm *v1.ConfigMap) {
	partitionID, generation, chunkIndex, chunkCount, ok := parseChunkLabels(cm.Labels)
	if !ok {
		klog.V(3).InfoS("Ignoring chunk ConfigMap with missing/invalid labels",
			"configmap", cm.Name)
		return
	}
	data, exists := cm.Data["snapshot"]
	if !exists {
		klog.V(3).InfoS("Chunk ConfigMap has no 'snapshot' key",
			"configmap", cm.Name)
		return
	}
	var snap libparsync.Snapshot
	if err := json.Unmarshal([]byte(data), &snap); err != nil {
		klog.ErrorS(err, "Failed to unmarshal chunk snapshot",
			"configmap", cm.Name)
		return
	}
	// Reject chunks whose Timestamp predates this consumer's creation:
	// they carry stale data from a previous experiment's Binder.
	if snap.Timestamp.Before(c.startedAt) {
		klog.V(3).InfoS("Rejected pre-startup chunk",
			"configmap", cm.Name, "partition", partitionID,
			"generation", generation, "snapshotTime", snap.Timestamp,
			"startedAt", c.startedAt)
		return
	}
	// Sanity: label-derived coordinates must match chunk body.
	if snap.PartitionID != partitionID || snap.Generation != generation {
		klog.InfoS("Chunk label/body mismatch",
			"configmap", cm.Name, "labelPartition", partitionID,
			"labelGeneration", generation, "bodyPartition", snap.PartitionID,
			"bodyGeneration", snap.Generation)
		return
	}

	c.pendingMu.Lock()
	defer c.pendingMu.Unlock()

	buf, exists := c.chunkBuffers[partitionID]
	if !exists || buf.generation < generation {
		// First chunk of a new generation — reset the buffer.
		buf = &chunkSet{
			generation: generation,
			chunkCount: chunkCount,
			chunks:     make(map[int]*libparsync.Snapshot, chunkCount),
		}
		c.chunkBuffers[partitionID] = buf
	} else if buf.generation > generation {
		// Chunk belongs to a generation already superseded; ignore.
		return
	}
	// chunkCount can legitimately differ between chunks only if a newer
	// publish used a different count. Trust the latest label we see.
	buf.chunkCount = chunkCount
	buf.chunks[chunkIndex] = &snap

	if len(buf.chunks) != buf.chunkCount {
		klog.V(5).InfoS("Chunk buffered, awaiting remaining chunks",
			"partition", partitionID, "generation", generation,
			"have", len(buf.chunks), "need", buf.chunkCount)
		return
	}

	// Complete: merge and promote.
	merged := mergeChunkSet(buf)
	c.pendingSnapshots[partitionID] = merged
	delete(c.chunkBuffers, partitionID)
	klog.InfoS("Merged complete chunk set into pending snapshot",
		"partition", partitionID, "generation", generation,
		"chunks", buf.chunkCount, "nodes", len(merged.Nodes))
}

// parseChunkLabels extracts the four label-encoded coordinates plus the
// chunk count from a ConfigMap's labels. Returns ok=false if any label is
// missing or fails to parse.
func parseChunkLabels(labels map[string]string) (partitionID int, generation int64, chunkIndex, chunkCount int, ok bool) {
	if labels == nil {
		return 0, 0, 0, 0, false
	}
	pidStr, ok1 := labels[libparsync.SnapshotLabelPartitionID]
	genStr, ok2 := labels[libparsync.SnapshotLabelGeneration]
	idxStr, ok3 := labels[libparsync.SnapshotLabelChunkIndex]
	cntStr, ok4 := labels[libparsync.SnapshotLabelChunkCount]
	if !ok1 || !ok2 || !ok3 || !ok4 {
		return 0, 0, 0, 0, false
	}
	pid, err1 := strconv.Atoi(pidStr)
	gen, err2 := strconv.ParseInt(genStr, 10, 64)
	idx, err3 := strconv.Atoi(idxStr)
	cnt, err4 := strconv.Atoi(cntStr)
	if err1 != nil || err2 != nil || err3 != nil || err4 != nil {
		return 0, 0, 0, 0, false
	}
	if pid < 0 || gen < 0 || idx < 0 || cnt <= 0 || idx >= cnt {
		return 0, 0, 0, 0, false
	}
	return pid, gen, idx, cnt, true
}

// mergeChunkSet concatenates the Nodes lists of all chunks into a single
// libparsync.Snapshot. PartitionID/Generation come from any chunk (they're
// identical by construction); Timestamp is taken as the max so downstream
// freshness calculations reflect the most recent chunk.
func mergeChunkSet(buf *chunkSet) *libparsync.Snapshot {
	indices := make([]int, 0, len(buf.chunks))
	for i := range buf.chunks {
		indices = append(indices, i)
	}
	sort.Ints(indices)

	var merged libparsync.Snapshot
	merged.PartitionID = buf.chunks[indices[0]].PartitionID
	merged.Generation = buf.generation
	merged.Timestamp = buf.chunks[indices[0]].Timestamp
	totalNodes := 0
	for _, i := range indices {
		totalNodes += len(buf.chunks[i].Nodes)
	}
	merged.Nodes = make([]libparsync.SnapshotNodeEntry, 0, totalNodes)
	for _, i := range indices {
		chunk := buf.chunks[i]
		if chunk.Timestamp.After(merged.Timestamp) {
			merged.Timestamp = chunk.Timestamp
		}
		merged.Nodes = append(merged.Nodes, chunk.Nodes...)
	}
	return &merged
}

// waitForInitialSnapshots blocks until every partition has at least one
// pending snapshot whose Timestamp >= startedAt, or until a timeout expires.
// This ensures the scheduler doesn't begin applying snapshots (and
// overwriting correct Informer data) with stale cross-experiment data.
// Timeout is generous (3 × syncPeriod, min 5s) — in normal operation the
// Binder publishes all partitions within one heartbeat period (~1s).
func (c *paraSyncConsumer) waitForInitialSnapshots(ctx context.Context) {
	timeout := c.syncPeriod * 3
	if timeout < 5*time.Second {
		timeout = 5 * time.Second
	}

	deadline := time.After(timeout)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	klog.InfoS("Waiting for initial partition snapshots",
		"partitions", c.numPartitions, "timeout", timeout, "startedAt", c.startedAt)

	for {
		select {
		case <-ctx.Done():
			close(c.readyCh)
			return
		case <-deadline:
			klog.InfoS("Initial snapshot wait timed out, proceeding with available data",
				"timeout", timeout, "received", c.countReadyPartitions())
			close(c.readyCh)
			return
		case <-ticker.C:
			if c.countReadyPartitions() >= c.numPartitions {
				klog.InfoS("All partitions received initial snapshot",
					"partitions", c.numPartitions,
					"elapsed", time.Since(c.startedAt))
				close(c.readyCh)
				return
			}
		}
	}
}

// countReadyPartitions returns how many partitions have a pending snapshot
// with a Timestamp >= startedAt.
func (c *paraSyncConsumer) countReadyPartitions() int {
	c.pendingMu.Lock()
	defer c.pendingMu.Unlock()
	count := 0
	for _, snap := range c.pendingSnapshots {
		if snap != nil && !snap.Timestamp.Before(c.startedAt) {
			count++
		}
	}
	return count
}

// rotationLoop runs the rotation window: every syncPeriod/numPartitions,
// advance to the next partition and apply its pending snapshot.
//
// Tick boundaries are aligned to the wall clock via libparsync.NextWallClockTick,
// so that N schedulers booted at different times still observe the same absolute
// slot boundaries. This preserves ParSync's "at wall-clock slot K, each scheduler
// syncs partitionOrder[K % len]" stagger invariant independent of startup drift.
//
// On every tick, it also unconditionally wakes pods in UnschedulableQ.
// This is necessary because even without new Binder snapshots, Informer
// events continuously update the cache (confirming assumed pods, freeing
// resources). Without periodic wakeup, pods that entered UnschedulableQ
// after the last snapshot application would be stuck for up to
// DefaultPodMaxInUnschedulableQDuration (5 minutes).
func (c *paraSyncConsumer) rotationLoop(ctx context.Context) {
	if c.numPartitions == 0 || len(c.partitionOrder) == 0 {
		return
	}

	slotDuration := c.syncPeriod / time.Duration(c.numPartitions)
	logger := klog.Background()

	for {
		nextTick := libparsync.NextWallClockTick(time.Now(), slotDuration)
		timer := time.NewTimer(time.Until(nextTick))

		select {
		case <-timer.C:
			slot := libparsync.WallClockSlot(time.Now(), slotDuration)
			partitionID := c.partitionOrder[int(slot%int64(len(c.partitionOrder)))]
			c.applyPendingSnapshot(partitionID)

			// Unconditionally wake UnschedulableQ pods on every rotation
			// tick. Informer events (pod confirms, node updates) may have
			// freed resources since the pods were last tried, even if the
			// Binder hasn't published a new snapshot.
			if c.sched.SchedulingQueue != nil {
				c.sched.SchedulingQueue.MoveAllToActiveOrBackoffQueue(
					logger, framework.EventForceActivate, nil, nil, nil)
			}

		case <-ctx.Done():
			timer.Stop()
			return
		}
	}
}

// applyPendingSnapshot takes the latest cached snapshot for a partition
// and applies it to the scheduler's node info snapshot.
func (c *paraSyncConsumer) applyPendingSnapshot(partitionID int) {
	c.pendingMu.Lock()
	snap, ok := c.pendingSnapshots[partitionID]
	if ok {
		delete(c.pendingSnapshots, partitionID)
	}
	c.pendingMu.Unlock()

	if !ok || snap == nil {
		return // No new snapshot for this partition.
	}

	klog.InfoS("Applying pending partition snapshot",
		"partition", partitionID, "generation", snap.Generation,
		"nodes", len(snap.Nodes), "staleness", time.Since(snap.Timestamp))

	// Timestamp check: reject snapshots created before this consumer started.
	// Such snapshots carry data from a previous experiment's Binder and would
	// overwrite correct Informer-based cache state with stale resource info.
	if snap.Timestamp.Before(c.startedAt) {
		klog.InfoS("Rejected pre-startup snapshot in apply",
			"partition", partitionID, "generation", snap.Generation,
			"snapshotTime", snap.Timestamp, "startedAt", c.startedAt)
		return
	}

	// Generation check: reject stale snapshots.
	firstApply := false
	if lastGen, exists := c.appliedGenerations[partitionID]; exists && snap.Generation <= lastGen {
		klog.InfoS("Rejected stale partition snapshot",
			"partition", partitionID, "generation", snap.Generation, "lastApplied", lastGen)
		return
	} else if !exists {
		firstApply = true
	}
	c.appliedGenerations[partitionID] = snap.Generation

	start := time.Now()

	// Record staleness: how old the snapshot is when we apply it.
	staleness := time.Since(snap.Timestamp)
	schedmetrics.ParaSchedPartitionStaleness.Observe(staleness.Seconds())

	// Apply snapshot entries to the scheduler's cache (not snapshot directly).
	// Writing to cache bumps the node's Generation, so the next UpdateSnapshot
	// will clone the authoritative data into the snapshot.
	logger := klog.Background()
	updated := 0
	for _, entry := range snap.Nodes {
		requested := framework.Resource{
			MilliCPU: entry.Requested.MilliCPU,
			Memory:   entry.Requested.Memory,
		}
		allocatable := framework.Resource{
			MilliCPU: entry.Allocatable.MilliCPU,
			Memory:   entry.Allocatable.Memory,
		}
		if err := c.sched.Cache.UpdateNodeResources(logger, entry.NodeName, requested, allocatable); err != nil {
			// Node not in cache — skip.
			continue
		}
		updated++
	}

	schedmetrics.ParaSchedSyncDuration.Observe(schedmetrics.SinceInSeconds(start))

	// Cold-start observability: record when each partition's first snapshot
	// was applied. The latest of these (max across partitions) is the
	// cold-start completion time per docs/design-parsync-pull-fix.md §4.6.4.
	if firstApply {
		schedmetrics.ParaSchedFirstSnapshotApplied.
			WithLabelValues(strconv.Itoa(partitionID)).
			SetToCurrentTime()
	}

	// Note: UnschedulableQ wakeup is handled by rotationLoop unconditionally
	// on every tick, not here. This avoids the gap where pods enter
	// UnschedulableQ after the last snapshot but before the next one.

	// Record sync time for freshness scoring.
	c.sched.UpdatePartitionSyncTime(partitionID, snap.Timestamp)

	klog.InfoS("Applied partition snapshot",
		"partition", partitionID, "generation", snap.Generation,
		"nodes", len(snap.Nodes), "updated", updated, "staleness", staleness)
}

// ---- AdoptionStats CRD watcher ----

// adoptionStatsWatcher watches the AdoptionStats CRD and feeds conflict rates
// to the scheduler's penalty mechanism.
type adoptionStatsWatcher struct {
	sched      *Scheduler
	paraClient parasched.Interface
	statsName  string // AdoptionStats CR name, default "default"
	probCalc   *libstats.ProbabilityCalculator
}

// newAdoptionStatsWatcher creates a new watcher.
func newAdoptionStatsWatcher(
	sched *Scheduler,
	paraClient parasched.Interface,
	statsName string,
) *adoptionStatsWatcher {
	if statsName == "" {
		statsName = "default"
	}
	return &adoptionStatsWatcher{
		sched:      sched,
		paraClient: paraClient,
		statsName:  statsName,
		probCalc:   libstats.NewProbabilityCalculator(),
	}
}

// Run starts the AdoptionStats Informer and blocks until ctx is cancelled.
func (w *adoptionStatsWatcher) Run(ctx context.Context) {
	factory := parainformers.NewSharedInformerFactory(w.paraClient, 10*time.Second)
	informer := factory.Scheduling().V1().AdoptionStatses().Informer()

	informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: func(obj interface{}) {
			w.handleUpdate(obj)
		},
		UpdateFunc: func(_, newObj interface{}) {
			w.handleUpdate(newObj)
		},
	})

	factory.Start(ctx.Done())
	factory.WaitForCacheSync(ctx.Done())

	// Block until context is cancelled.
	<-ctx.Done()
}

// handleUpdate extracts conflict rates from AdoptionStats and updates the scheduler.
func (w *adoptionStatsWatcher) handleUpdate(obj interface{}) {
	stats, ok := obj.(*apisv1.AdoptionStats)
	if !ok {
		return
	}
	if stats.Name != w.statsName {
		return
	}

	status := &stats.Status
	if status.TotalBindings == 0 {
		return
	}

	// Compute global conflict rate via scheduler-lib ProbabilityCalculator.
	// Kept as a fallback for nodes not covered by NodeCounts / PartitionCounts.
	globalConflictRate := w.probCalc.CalculateConflictRate(status.SuccessCount, status.FailureCount)

	// Build per-node rates with a 3-level fallback:
	//   1. NodeCounts[node] = [attempts, conflicts]  (per-node, preferred — true per-node
	//      signal reported by Binder after the candidate-level-failure fix)
	//   2. PartitionCounts (partition granularity, only useful when partitions differ)
	//   3. globalConflictRate (cluster-wide, effectively a constant — no differentiation)
	//
	// NodeCounts is sparse: absent nodes have had no recorded conflicts recently, so we
	// safely default them to 0 (Penalty formula: (1-p)*score + p*(1-0) = (1-p)*score + p
	// — same constant offset for all zero-conflict nodes, still no ordering impact).
	rates := make(map[string]float64)

	// Get all node names from the scheduler's snapshot.
	allNodes, err := w.sched.nodeInfoSnapshot.NodeInfos().List()
	if err != nil {
		klog.ErrorS(err, "Failed to list nodes for conflict rate update")
		return
	}

	nodeCountsPresent := len(status.NodeCounts) > 0
	for _, ni := range allNodes {
		nodeName := ni.Node().Name

		// Level 1: per-node NodeCounts (preferred).
		if nc, ok := status.NodeCounts[nodeName]; ok && len(nc) >= 2 {
			attempts, conflicts := nc[0], nc[1]
			if attempts > 0 {
				rates[nodeName] = float64(conflicts) / float64(attempts)
				continue
			}
		}
		// If Binder populates NodeCounts at all, absent-node means zero conflicts,
		// so don't fall through to partition/global (which would incorrectly mark
		// healthy nodes with the cluster-wide conflict rate).
		if nodeCountsPresent {
			rates[nodeName] = 0
			continue
		}

		// Level 2: legacy partition-level fallback (Binder without per-candidate
		// reporting). Retained for backward compatibility with older Binder builds.
		if pid := getNodePartitionID(ni); pid >= 0 && status.PartitionCounts != nil {
			pKey := strconv.Itoa(pid)
			if pc, ok := status.PartitionCounts[pKey]; ok {
				var pSuccess int64
				for _, count := range pc {
					pSuccess += count
				}
				var pTotal int64
				if status.SuccessCount > 0 {
					pTotal = status.TotalBindings * pSuccess / status.SuccessCount
				}
				if pTotal > 0 {
					pFail := pTotal - pSuccess
					rates[nodeName] = w.probCalc.CalculateConflictRate(pSuccess, pFail)
					continue
				}
			}
		}

		// Level 3: global conflict rate (final fallback).
		rates[nodeName] = globalConflictRate
	}

	// Record how old the conflict-rate feed already is when we install it.
	// Deliberately observed here rather than in GetNodeConflictRate: this
	// mirrors ParaSchedPartitionStaleness (observed when a snapshot is
	// applied), so the two pipelines are measured under the same convention,
	// and it keeps the per-node scoring path free of instrumentation.
	if status.LastUpdateTime != nil {
		schedmetrics.ParaSchedPenaltySignalAge.Observe(time.Since(status.LastUpdateTime.Time).Seconds())
	}

	w.sched.UpdateConflictRates(rates)

	klog.V(4).InfoS("Updated conflict rates from AdoptionStats",
		"globalRate", globalConflictRate, "nodes", len(rates),
		"nodeCountsEntries", len(status.NodeCounts))
}

// getNodePartitionID extracts the partition ID from node labels/annotations.
// Returns -1 if not available.
func getNodePartitionID(ni *framework.NodeInfo) int {
	node := ni.Node()
	if node == nil {
		return -1
	}
	if pidStr, ok := node.Labels["para-scheduler.io/partition-id"]; ok {
		pid, err := strconv.Atoi(pidStr)
		if err == nil {
			return pid
		}
	}
	return -1
}

// ---- Integration: start ParSync and AdoptionStats watcher from scheduler ----

// StartParaSyncConsumer starts the ParSync consumer goroutine.
// Called from server.go after scheduler is created and configured.
func (sched *Scheduler) StartParaSyncConsumer(
	ctx context.Context,
	kubeClient kubernetes.Interface,
	namespace string,
	numPartitions int,
	syncPeriod time.Duration,
	partitionOrder []int,
) {
	consumer := newParaSyncConsumer(kubeClient, sched, namespace, numPartitions, syncPeriod, partitionOrder)
	go consumer.Run(ctx)
	klog.FromContext(ctx).Info("Started ParSync consumer",
		"partitions", numPartitions, "syncPeriod", syncPeriod,
		"partitionOrder", partitionOrder)
}

// StartAdoptionStatsWatcher starts the AdoptionStats CRD watcher goroutine.
// Called from server.go after scheduler is created and configured.
func (sched *Scheduler) StartAdoptionStatsWatcher(
	ctx context.Context,
	paraClient parasched.Interface,
	statsName string,
) {
	watcher := newAdoptionStatsWatcher(sched, paraClient, statsName)
	go watcher.Run(ctx)
	klog.FromContext(ctx).Info("Started AdoptionStats watcher", "statsName", statsName)
}

// StartHeartbeatLoop periodically patches the SchedulerAssignment CRD's
// status.lastHeartbeat (and phase=Running) to signal liveness to the Dispatcher.
//
// Background: the Dispatcher's in-memory registry only sets LastHeartbeat at
// Register time. Without this loop, the Pod Informer's ready→ready updates
// do not refresh LastHeartbeat, so failoverTimeout (default 15s) expires and
// every scheduler is silently marked Failed — which then triggers spurious
// rebalances. Persisting the heartbeat on the CRD also lets a restarted
// Dispatcher recover registry state without waiting a full failoverTimeout.
//
// Patch uses the "status" subresource so that it never races with the
// Dispatcher's Spec updates (partition reassignments) on resourceVersion.
//
// If paraClient is nil, name is empty, or interval <= 0, returns immediately.
// Transient errors (NotFound while Dispatcher hasn't created the CRD yet,
// network blips) are logged at V(3) and retried on the next tick.
func (sched *Scheduler) StartHeartbeatLoop(
	ctx context.Context,
	paraClient parasched.Interface,
	name string,
	interval time.Duration,
) {
	if paraClient == nil || name == "" || interval <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		sendHeartbeat(ctx, paraClient, name)
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				sendHeartbeat(ctx, paraClient, name)
			}
		}
	}()
	klog.FromContext(ctx).Info("Started SchedulerAssignment heartbeat loop",
		"name", name, "interval", interval)
}

// sendHeartbeat patches status.lastHeartbeat=now and status.phase=Running
// using MergePatch on the "status" subresource.
func sendHeartbeat(ctx context.Context, client parasched.Interface, name string) {
	now := metav1.Now()
	patch, err := json.Marshal(map[string]interface{}{
		"status": map[string]interface{}{
			"lastHeartbeat": &now,
			"phase":         apisv1.SchedulerPhaseRunning,
		},
	})
	if err != nil {
		klog.ErrorS(err, "Failed to marshal heartbeat patch", "name", name)
		return
	}
	_, err = client.SchedulingV1().SchedulerAssignments().Patch(
		ctx, name, k8stypes.MergePatchType, patch, metav1.PatchOptions{}, "status")
	if err != nil {
		klog.V(3).InfoS("SchedulerAssignment heartbeat patch failed",
			"name", name, "err", err)
	}
}
